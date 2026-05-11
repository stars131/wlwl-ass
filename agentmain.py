import os, sys, threading, queue, time, json, re, random, locale
from dataclasses import dataclass
os.environ.setdefault('WLWL_LANG', 'zh' if any(k in (locale.getlocale()[0] or '').lower() for k in ('zh', 'chinese')) else 'en')
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from llmcore import reload_mykeys, LLMSession, ToolClient, ClaudeSession, MixinSession, NativeToolClient, NativeClaudeSession, NativeOAISession
from agent_loop import agent_runner_loop, ensure_safe_std_streams
ensure_safe_std_streams()  # .pyw / 后台模式下 sys.stdout/stderr 可能为 None

# Optional langfuse tracing — self-disables when settings.langfuse is unset.
# Imported here (not inside _keys.reload_mykeys) so the patcher runs once per
# process and won't re-fire on cache busts.
try:
    from plugins import langfuse_tracing  # noqa: F401 — side-effect tracer install
except ImportError:
    pass  # plugin (or langfuse SDK) not installed — silently skip
except Exception as _lf_exc:  # init bug — don't kill the agent, but surface once
    print(f"[wlwl-ass] langfuse_tracing init failed (non-fatal): {_lf_exc!r}", file=sys.stderr)

from wlwl_ass import WlwlAssHandler, smart_format, get_global_memory, format_error, consume_file
from permissions import InteractivePermissionPrompter, PermissionPolicy
from project_context import load_project_context
try:
    from cli_commands import SharedCommandHandler
except ImportError:
    sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontends'))
    from cli_commands import SharedCommandHandler

script_dir = os.path.dirname(os.path.abspath(__file__))
def load_tool_schema(suffix=''):
    global TOOLS_SCHEMA
    with open(os.path.join(script_dir, f'assets/tools_schema{suffix}.json'), 'r', encoding='utf-8') as _f:
        TS = _f.read()
    TOOLS_SCHEMA = json.loads(TS if os.name == 'nt' else TS.replace('powershell', 'bash'))
load_tool_schema()

lang_suffix = '_en' if os.environ.get('WLWL_LANG', '') == 'en' else ''
mem_dir = os.path.join(script_dir, 'memory')
if not os.path.exists(mem_dir): os.makedirs(mem_dir)
mem_txt = os.path.join(mem_dir, 'global_mem.txt')
if not os.path.exists(mem_txt):
    with open(mem_txt, 'w', encoding='utf-8') as _f: _f.write('# [Global Memory - L2]\n')
mem_insight = os.path.join(mem_dir, 'global_mem_insight.txt')
if not os.path.exists(mem_insight):
    t = os.path.join(script_dir, f'assets/global_mem_insight_template{lang_suffix}.txt')
    template_text = ''
    if os.path.exists(t):
        with open(t, encoding='utf-8') as _f: template_text = _f.read()
    with open(mem_insight, 'w', encoding='utf-8') as _f: _f.write(template_text)
cdp_cfg = os.path.join(script_dir, 'assets/tmwd_cdp_bridge/config.js')
if not os.path.exists(cdp_cfg):
    try:
        os.makedirs(os.path.dirname(cdp_cfg), exist_ok=True)
        # 固定 6 位小写十六进制，避免 hex(randint(...))[2:8] 在小数值时不足 6 位
        tid_suffix = f"{random.randint(0, 0xFFFFFF):06x}"
        with open(cdp_cfg, 'w', encoding='utf-8') as f:
            f.write(f"const TID = '__ljq_{tid_suffix}';")
    except Exception as e: print(f'[WARN] CDP config init failed: {e} — advanced web features (tmwebdriver) will be unavailable.')

def get_system_prompt():
    with open(os.path.join(script_dir, f'assets/sys_prompt{lang_suffix}.txt'), 'r', encoding='utf-8') as f: prompt = f.read()
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += get_global_memory()
    return prompt

@dataclass(frozen=True)
class AgentRuntimeContext:
    project_id: str = ''
    project_name: str = ''
    project_root: str = ''
    llm_no: int = 0
    llm_config_name: str = ''
    permission_mode: str = 'auto'
    use_project_context: bool = True
    autonomous_enabled: bool = False
    resume_task_id: str | None = None


class GeneraticAgent:
    def __init__(self, runtime_context: AgentRuntimeContext | None = None):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(os.path.join(script_dir, 'temp'), exist_ok=True)
        self.runtime_context = runtime_context
        self.lock = threading.Lock()
        self.task_dir = None
        self.history = []
        self.task_queue = queue.Queue()
        self.is_running = False; self.stop_sig = False
        self.llm_no = int(getattr(runtime_context, 'llm_no', 0) or 0);  self.inc_out = False
        self.handler = None; self.verbose = True
        self.permission_mode = 'auto'
        self.permission_policy = None
        self.permission_prompter = None
        self.project_root = None
        self.project_context = None
        self.use_project_context = False
        self.cli_mode = False
        self._turn_end_hooks = {}  # name → callable; populated below + by extensions
        self.load_llm_sessions()
        # Auto-checkpoint hook (launcher/auto_checkpoint.py). Saves working
        # state to temp/checkpoints/ every 5 turns + on exit so a context
        # flush / kill mid-task can be resumed via the `checkpoint` tool.
        # Failure to install is non-fatal — the agent still runs without
        # snapshots, just less safely on long tasks.
        try:
            from launcher.auto_checkpoint import install_auto_checkpoint, maybe_apply_resume
            resume_task_id = getattr(runtime_context, 'resume_task_id', None) if runtime_context else None
            project_id = getattr(runtime_context, 'project_id', '') if runtime_context else ''
            self._auto_checkpoint_task_id = install_auto_checkpoint(
                self,
                task_id=resume_task_id,
                project_id=project_id,
            )
            maybe_apply_resume(self, task_id=resume_task_id)
        except Exception as exc:
            print(f"[GeneraticAgent] auto_checkpoint install failed: {exc}")
        if runtime_context is not None:
            self.apply_runtime_context(runtime_context)

    def load_llm_sessions(self):
        project_root = getattr(self.runtime_context, 'project_root', None) if self.runtime_context else None
        mykeys, changed = reload_mykeys(project_root=project_root)
        if not changed and hasattr(self, 'llmclients'): return
        try: oldhistory = self.llmclient.backend.history
        except (AttributeError, TypeError): oldhistory = None
        llm_sessions = []
        for k, cfg in mykeys.items():
            if not any(x in k for x in ['api', 'config', 'cookie']): continue
            try:
                if 'native' in k and 'claude' in k: llm_sessions += [NativeToolClient(NativeClaudeSession(cfg=cfg))]
                elif 'native' in k and 'oai' in k: llm_sessions += [NativeToolClient(NativeOAISession(cfg=cfg))]
                elif 'claude' in k: llm_sessions += [ToolClient(ClaudeSession(cfg=cfg))]
                elif 'oai' in k: llm_sessions += [ToolClient(LLMSession(cfg=cfg))]
                elif 'mixin' in k: llm_sessions += [{'mixin_cfg': cfg}]
            except Exception as _e: print(f'[WARN] init LLM session {k!r} failed: {_e}')
        for i, s in enumerate(llm_sessions):
            if isinstance(s, dict) and 'mixin_cfg' in s:
                try:
                    mixin = MixinSession(llm_sessions, s['mixin_cfg'])
                    if isinstance(mixin._sessions[0], (NativeClaudeSession, NativeOAISession)): llm_sessions[i] = NativeToolClient(mixin)
                    else: llm_sessions[i] = ToolClient(mixin)
                except Exception as e: print(f'[WARN] Failed to init MixinSession with cfg {s["mixin_cfg"]}: {e}')
        self.llmclients = llm_sessions
        self.llmclient = self.llmclients[self.llm_no%len(self.llmclients)]
        if oldhistory: self.llmclient.backend.history = oldhistory

    def next_llm(self, n=-1):
        self.load_llm_sessions()
        self.llm_no = ((self.llm_no + 1) if n < 0 else n) % len(self.llmclients)
        lastc = self.llmclient
        self.llmclient = self.llmclients[self.llm_no]
        try: self.llmclient.backend.history = lastc.backend.history
        except (AttributeError, TypeError) as _e:
            raise Exception('[ERROR] BAD Mixin config: Check your launcher_api_configs.json (GUI > API 配置 tab) or `python -m launcher.config get providers`') from _e
        self.llmclient.last_tools = ''
        name = self.get_llm_name(model=True)
        if 'glm' in name or 'minimax' in name or 'kimi' in name: load_tool_schema('_cn')
        else: load_tool_schema()

    def select_llm_by_name(self, config_name):
        """ADR-0006: per-session API config selection by `name` rather than index.

        Returns True if a client with matching `name` was found and made active,
        False otherwise (caller should fall back to llm_no).
        """
        self.load_llm_sessions()
        target = (config_name or '').strip()
        if not target:
            return False
        for i, client in enumerate(self.llmclients):
            try:
                cname = getattr(client.backend, 'name', '') or ''
            except Exception:
                cname = ''
            if cname == target:
                self.next_llm(i)
                return True
        return False

    def list_llms(self):
        self.load_llm_sessions()
        return [(i, self.get_llm_name(b), i == self.llm_no) for i, b in enumerate(self.llmclients)]
    def apply_runtime_context(self, ctx):
        llm_name = str(getattr(ctx, 'llm_config_name', '') or '').strip()
        if llm_name:
            selected = self.select_llm_by_name(llm_name)
            if not selected:
                self.next_llm(int(getattr(ctx, 'llm_no', 0) or 0))
        else:
            self.next_llm(int(getattr(ctx, 'llm_no', 0) or 0))
        project_root = str(getattr(ctx, 'project_root', '') or '').strip() or None
        self.configure_cli(
            permission_mode=str(getattr(ctx, 'permission_mode', 'auto') or 'auto'),
            project_root=project_root,
            use_project_context=bool(getattr(ctx, 'use_project_context', True)),
            interactive=False,
            cwd_project=True,
        )
        self.inc_out = True
    def get_llm_name(self, b=None, model=False):
        b = self.llmclient if b is None else b
        if isinstance(b, dict): return 'BADCONFIG_MIXIN'
        if model: return b.backend.model.lower()
        return f"{type(b.backend).__name__}/{b.backend.name}"

    def abort(self):
        if not self.is_running: return
        print('Abort current task...')
        self.stop_sig = True
        if self.handler is not None: self.handler.code_stop_signal.append(1)

    def shutdown(self):
        """Stop the background run loop after the current task is aborted."""
        self.abort()
        self.task_queue.put({"_shutdown": True})
            
    def put_task(self, query, source="user", images=None):
        display_queue = queue.Queue()
        self.task_queue.put({"query": query, "source": source, "images": images or [], "output": display_queue})
        return display_queue

    def _handle_slash_cmd(self, raw_query, display_queue):
        if not raw_query.startswith('/'):
            return raw_query
        result = SharedCommandHandler(self).handle(raw_query)
        if result.handled:
            display_queue.put({'done': result.message or '', 'source': 'system'})
            return None
        return result.query

    def configure_cli(self, permission_mode='ask', project_root=None, use_project_context=True, interactive=True, cwd_project=None):
        self.cli_mode = interactive if cwd_project is None else bool(cwd_project)
        self.permission_mode = permission_mode
        self.project_context = load_project_context(project_root or os.getcwd(), enabled=use_project_context)
        self.project_root = self.project_context.root
        self.use_project_context = use_project_context
        self.permission_policy = PermissionPolicy(mode=permission_mode, interactive=interactive)
        self.permission_prompter = InteractivePermissionPrompter(self.permission_policy) if interactive else None

    def run(self):
        while True:
            task = self.task_queue.get()
            if task.get("_shutdown"):
                self.task_queue.task_done()
                break
            raw_query, source, images, display_queue = task["query"], task["source"], task.get("images") or [], task["output"]
            raw_query = self._handle_slash_cmd(raw_query, display_queue)
            if raw_query is None:
                self.task_queue.task_done(); continue
            self.is_running = True
            rquery = smart_format(raw_query.replace('\n', ' '), max_str_len=200)
            self.history.append(f"[USER]: {rquery}")

            # task_id：跨多个 turn 关联同一次用户请求，便于按任务聚合指标
            import uuid as _uuid
            task_id = _uuid.uuid4().hex[:12]
            self._current_task_id = task_id
            task_started_mono = time.monotonic()
            try:
                from launcher import activity_log as _alog
                _alog.record({
                    'phase': 'task_start',
                    'task_id': task_id,
                    'source': source,
                    'query_preview': rquery,
                    'llm': self.get_llm_name() if self.llmclient else '',
                    'cli_mode': self.cli_mode,
                })
            except Exception:
                pass
            
            sys_prompt = get_system_prompt() + getattr(self.llmclient.backend, 'extra_sys_prompt', '')
            if self.use_project_context and self.project_context and self.project_context.text:
                sys_prompt += "\n" + self.project_context.text
            script_dir = os.path.dirname(os.path.abspath(__file__))
            cwd = self.project_root if self.cli_mode and self.project_root else os.path.join(script_dir, 'temp')
            handler = WlwlAssHandler(self, self.history, cwd, permission_policy=self.permission_policy,
                                          permission_prompter=self.permission_prompter, project_root=self.project_root)
            if self.handler and 'key_info' in self.handler.working:
                ki = re.sub(r'\n\[SYSTEM\] 此为.*?工作记忆[。\n]*', '', self.handler.working['key_info'])  # 去旧
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = ps = self.handler.working.get('passed_sessions', 0) + 1
                if ps > 0: handler.working['key_info'] += f'\n[SYSTEM] 此为 {ps} 个对话前设置的key_info，若已在新任务，先更新或清除工作记忆。\n'
            # Auto-checkpoint resume: if launcher.auto_checkpoint.maybe_apply_resume
            # parked a state on the agent, drain it now into the handler.
            # One-shot — clear the pending state so the next task doesn't
            # re-inject stale context. Failure here is non-fatal.
            pending_resume = getattr(self, '_pending_resume_state', None)
            if pending_resume:
                try:
                    resume_turn = pending_resume.get('turn')
                    resume_ki = (pending_resume.get('key_info') or '').strip()
                    resume_sop = (pending_resume.get('related_sop') or '').strip()
                    history_tail = pending_resume.get('history_tail') or []
                    marker = f"\n[RESUME from turn {resume_turn}] 上次会话被中断，以下是最近上下文 + 工作记忆。继续推进或先用 update_working_checkpoint 整合。"
                    if resume_ki:
                        handler.working['key_info'] = (handler.working.get('key_info') or '') + '\n' + marker + '\n' + resume_ki
                    else:
                        handler.working['key_info'] = (handler.working.get('key_info') or '') + marker
                    if resume_sop:
                        handler.working['related_sop'] = resume_sop
                    if isinstance(history_tail, list) and history_tail:
                        # Prepend so the new turn's context block shows the
                        # tail; cap to 40 lines to match auto_checkpoint's
                        # save policy.
                        handler.history_info = list(history_tail)[-40:] + list(handler.history_info)
                except Exception as _exc:
                    print(f"[GeneraticAgent] resume injection failed: {_exc}")
                finally:
                    self._pending_resume_state = None
            self.handler = handler
            # although new handler, the **full** history is in llmclient, so it is full history!
            gen = agent_runner_loop(self.llmclient, sys_prompt, raw_query, 
                                handler, TOOLS_SCHEMA, max_turns=70, verbose=self.verbose)
            try:
                full_resp = ""; last_pos = 0
                # 阈值：inc_out 时按 50 字符细粒度推增量；否则放大到 2000 字符
                # 避免 O(n²) — 每次都把整段 full_resp 拷进 queue。
                push_threshold = 50 if self.inc_out else 2000
                for chunk in gen:
                    if consume_file(self.task_dir, '_stop'): self.abort()
                    if self.stop_sig: break
                    full_resp += chunk
                    if len(full_resp) - last_pos > push_threshold or 'LLM Running' in chunk:
                        display_queue.put({'next': full_resp[last_pos:] if self.inc_out else full_resp, 'source': source})
                        last_pos = len(full_resp)
                if self.inc_out and last_pos < len(full_resp): display_queue.put({'next': full_resp[last_pos:], 'source': source})
                if '</summary>' in full_resp: full_resp = full_resp.replace('</summary>', '</summary>\n\n')
                if '</file_content>' in full_resp: full_resp = re.sub(r'<file_content>\s*(.*?)\s*</file_content>', r'\n````\n<file_content>\n\1\n</file_content>\n````', full_resp, flags=re.DOTALL)                
                display_queue.put({'done': full_resp, 'source': source})
                self.history = handler.history_info
            except Exception as e:
                self._task_had_exception = True
                print(f"Backend Error: {format_error(e)}")
                display_queue.put({'done': full_resp + f'\n```\n{format_error(e)}\n```', 'source': source})
            finally:
                if self.stop_sig:
                    print('User aborted the task.')
                # 任务结束事件：完整闭环，覆盖 success / aborted / max_turns / exception
                try:
                    from launcher import activity_log as _alog
                    _outcome = 'aborted' if self.stop_sig else (
                        'completed' if not getattr(self, '_task_had_exception', False) else 'failed')
                    _alog.record({
                        'phase': 'task_end',
                        'task_id': task_id,
                        'source': source,
                        'outcome': _outcome,
                        'turns': getattr(self.handler, 'current_turn', 0) if self.handler else 0,
                        'elapsed_s': round(time.monotonic() - task_started_mono, 3),
                        'llm': self.get_llm_name() if self.llmclient else '',
                    })
                except Exception:
                    pass
                self._task_had_exception = False
                self.is_running = self.stop_sig = False
                self.task_queue.task_done()
                if self.handler is not None: self.handler.code_stop_signal.append(1)


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(seconds, 60)
    return f"{int(minutes)}m {int(sec):02d}s"


if __name__ == '__main__':
    import argparse
    from datetime import datetime
    parser = argparse.ArgumentParser(
        description='Internal agent entry: headless --task / --reflect / --bg modes only. '
                    'For interactive use, launch the GUI via start_from_zero.cmd or `python launch.pyw`.'
    )
    parser.add_argument('--task', metavar='IODIR', help='一次性任务模式(文件IO)')
    parser.add_argument('--reflect', metavar='SCRIPT', help='反射模式：加载监控脚本，check()触发时发任务')
    parser.add_argument('--input', help='prompt')
    parser.add_argument('--llm_no', type=int, default=0)
    parser.add_argument('--bg', action='store_true', help='popen, print PID, exit')
    args = parser.parse_args()

    if not (args.task or args.reflect or args.bg):
        print('agentmain.py is now an internal entry. Use start_from_zero.cmd '
              'or `python launch.pyw` to start the Tauri GUI. Headless modes: '
              '--task / --reflect / --bg.', file=sys.stderr)
        sys.exit(2)

    if args.bg:
        import subprocess, platform
        cmd = [sys.executable, os.path.abspath(__file__)] + [a for a in sys.argv[1:] if a != '--bg']
        d = os.path.join(script_dir, f'temp/{args.task}'); os.makedirs(d, exist_ok=True)
        # 子进程 stdio 文件：用 with 持有，subprocess.Popen 会 dup 句柄给子进程，
        # 父进程退出 with 时关闭自己的副本，避免句柄泄漏给后续 sys.exit。
        with open(os.path.join(d, 'stdout.log'), 'w', encoding='utf-8') as _so, \
             open(os.path.join(d, 'stderr.log'), 'w', encoding='utf-8') as _se:
            p = subprocess.Popen(cmd, cwd=script_dir,
                creationflags=0x08000000 if platform.system() == 'Windows' else 0,
                stdout=_so, stderr=_se)
        print(p.pid); sys.exit(0)

    agent = GeneraticAgent()
    agent.next_llm(args.llm_no)
    # 头less 模式（--task / --reflect）走 read-only 权限，project context 关闭：
    # 这些是后台批处理 / 调度场景，不应该自由动用户的项目目录。
    agent.configure_cli(permission_mode='read-only', project_root=None,
                        use_project_context=False, interactive=False)
    threading.Thread(target=agent.run, daemon=True).start()

    if args.task:
        agent.task_dir = d = os.path.join(script_dir, f'temp/{args.task}')
        # round_idx: 0 → 'output.txt'，之后 1/2/3 → 'output1.txt' / 'output2.txt' ...
        round_idx = 0
        infile = os.path.join(d, 'input.txt')
        if args.input:
            os.makedirs(d, exist_ok=True)
            import glob; [os.remove(f) for f in glob.glob(os.path.join(d, 'output*.txt'))]
            with open(infile, 'w', encoding='utf-8') as f: f.write(args.input)
        with open(infile, encoding='utf-8') as f: raw = f.read()
        while True:
            output_suffix = '' if round_idx == 0 else str(round_idx)
            output_path = f'{d}/output{output_suffix}.txt'
            dq = agent.put_task(raw, source='task')
            while 'done' not in (item := dq.get(timeout=120)):
                if 'next' in item and random.random() < 0.95:  # 概率写一次中间结果
                    with open(output_path, 'w', encoding='utf-8') as f: f.write(item.get('next', ''))
            with open(output_path, 'w', encoding='utf-8') as f: f.write(item['done'] + '\n\n[ROUND END]\n')
            consume_file(d, '_stop')  # 已经成功停下来了，避免打断下次reply
            for _ in range(300):  # 等reply.txt，10分钟超时
                time.sleep(2)
                if (raw := consume_file(d, 'reply.txt')): break
            else: break
            round_idx += 1
    elif args.reflect:
        import importlib.util
        spec = importlib.util.spec_from_file_location('reflect_script', args.reflect)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        _mt = os.path.getmtime(args.reflect)
        print(f'[Reflect] loaded {args.reflect}')
        while True:
            if os.path.getmtime(args.reflect) != _mt:
                try: spec.loader.exec_module(mod); _mt = os.path.getmtime(args.reflect); print('[Reflect] reloaded')
                except Exception as e: print(f'[Reflect] reload error: {e}')
            time.sleep(getattr(mod, 'INTERVAL', 5))
            try: task = mod.check()
            except Exception as e:
                print(f'[Reflect] check() error: {e}'); continue
            if task is None: continue
            print(f'[Reflect] triggered: {task[:80]}')
            dq = agent.put_task(task, source='reflect')
            try:
                while 'done' not in (item := dq.get(timeout=120)): pass
                result = item['done']
                print(result)
            except Exception as e:
                if getattr(mod, 'ONCE', False): raise
                print(f'[Reflect] drain error: {e}'); result = f'[ERROR] {e}'
            log_dir = os.path.join(script_dir, 'temp/reflect_logs'); os.makedirs(log_dir, exist_ok=True)
            script_name = os.path.splitext(os.path.basename(args.reflect))[0]
            log_path = os.path.join(log_dir, f'{script_name}_{datetime.now():%Y-%m-%d}.log')
            with open(log_path, 'a', encoding='utf-8') as _lf:
                _lf.write(f'[{datetime.now():%m-%d %H:%M}]\n{result}\n\n')
            if (on_done := getattr(mod, 'on_done', None)):
                try: on_done(result)
                except Exception as e: print(f'[Reflect] on_done error: {e}')
            if getattr(mod, 'ONCE', False): print('[Reflect] ONCE=True, exiting.'); break
