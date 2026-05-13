import sys, os, re, json, time, threading, importlib
from datetime import datetime
from pathlib import Path
import tempfile, traceback, subprocess, itertools, collections, difflib
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_loop import BaseHandler, StepOutcome, json_default, ensure_safe_std_streams
ensure_safe_std_streams()
from permissions import PermissionDecision, ToolPermissionRequest, tool_metadata
from launcher import activity_log


def _bool_arg(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _harvest_extract_text(resp):
    """``web_execute_js`` returns either a string (legacy) or a dict shaped
    like ``{"result": ...}`` / ``{"data": ...}``. Normalise to plain text so
    the forum harvester can feed it to ``json.loads`` without caring."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for key in ("text", "result", "data", "value", "body"):
            value = resp.get(key)
            if isinstance(value, str) and value.strip():
                return value
        # Some bridges return {"status": "success", "result": {"data": "..."}}
        nested = resp.get("result")
        if isinstance(nested, dict):
            for key in ("data", "value", "body", "text"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return ""

def code_run(code, code_type="python", timeout=60, cwd=None, code_cwd=None, stop_signal=None):
    """代码执行器
    python: 运行复杂的 .py 脚本（文件模式）
    powershell/bash: 运行单行指令（命令模式）
    优先使用python，仅在必要系统操作时使用powershell"""
    if stop_signal is None: stop_signal = []  # 修复可变默认参数共享问题
    preview = (code[:60].replace('\n', ' ') + '...') if len(code) > 60 else code.strip()
    yield f"[Action] Running {code_type} in {os.path.basename(cwd)}: {preview}\n"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = cwd or os.path.join(script_dir, 'temp'); tmp_path = None
    if code_type in ["python", "py"]:
        tmp_file = tempfile.NamedTemporaryFile(suffix=".ai.py", delete=False, mode='w', encoding='utf-8', dir=code_cwd)
        cr_header = os.path.join(script_dir, 'assets', 'code_run_header.py')
        if os.path.exists(cr_header):
            with open(cr_header, encoding='utf-8') as _hf: tmp_file.write(_hf.read())
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]
    elif code_type in ["powershell", "bash", "sh", "shell", "ps1", "pwsh"]:
        if os.name == 'nt': cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
        else: cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"不支持的类型: {code_type}"}
    print("code run output:") 
    startupinfo = None
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 # SW_HIDE
    full_stdout = []

    def stream_reader(proc, logs):
        try:
            for line_bytes in iter(proc.stdout.readline, b''):
                try: line = line_bytes.decode('utf-8')
                except UnicodeDecodeError: line = line_bytes.decode('gbk', errors='ignore')
                logs.append(line)
                try: print(line, end="")
                except (UnicodeEncodeError, OSError): pass  # 控制台编码/管道关闭等可忽略
        except Exception as _read_err:  # 进程退出 / IO 异常都吞掉，由外层 process.poll() 处理
            # 至少留一行 stderr 痕迹，避免静默调试黑箱
            try:
                print(f"[code_run stream_reader] {type(_read_err).__name__}: {_read_err}", file=sys.stderr)
            except (UnicodeEncodeError, OSError):
                pass

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, cwd=cwd, startupinfo=startupinfo
        )
        start_t = time.time()
        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while t.is_alive():
            istimeout = time.time() - start_t > timeout
            if istimeout or len(stop_signal) > 0:
                process.kill()
                print("[Debug] Process killed due to timeout or stop signal.")
                if istimeout: full_stdout.append("\n[Timeout Error] 超时强制终止")
                else: full_stdout.append("\n[Stopped] 用户强制终止")
                break
            time.sleep(1)

        t.join(timeout=1)
        exit_code = process.poll()

        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"
        status_icon = "✅" if exit_code == 0 else "❌"
        if exit_code is None: status_icon = "⏳" 
        output_snippet = smart_format(stdout_str, max_str_len=600, omit_str='\n\n[omitted long output]\n\n')
        yield f"[Status] {status_icon} Exit Code: {exit_code}\n[Stdout]\n{output_snippet}\n"
        if process.stdout: threading.Thread(target=process.stdout.close, daemon=True).start()
        return {
            "status": status,
            "stdout": smart_format(stdout_str, max_str_len=10000, omit_str='\n\n[omitted long output]\n\n'),
            "exit_code": exit_code
        }
    except Exception as e:
        if 'process' in locals(): process.kill()
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type == "python" and tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)


def ask_user(question, candidates=None):
    """question: 向用户提出的问题。candidates: 可选的候选项列表"""
    return {"status": "INTERRUPT", "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []}}

import simphtml
driver = None
def first_init_driver():
    global driver
    from TMWebDriver import TMWebDriver
    driver = TMWebDriver()
    for i in range(20):
        time.sleep(1)
        sess = driver.get_all_sessions()
        if len(sess) > 0: break
    if len(sess) == 0: return 
    if len(sess) == 1: 
        #driver.newtab()
        time.sleep(3)

def web_scan(tabs_only=False, switch_tab_id=None, text_only=False):
    """获取当前页面的简化HTML内容和标签页列表。注意：简化过程会过滤边栏、浮动元素等非主体内容。
    tabs_only: 仅返回标签页列表，不获取HTML内容（节省token）。
    switch_tab_id: 可选参数，如果提供，则在扫描前切换到该标签页。
    应当多用execute_js，少全量观察html"""
    global driver
    try:
        if driver is None: first_init_driver()
        if len(driver.get_all_sessions()) == 0:
            return {"status": "error", "msg": "没有可用的浏览器标签页，查L3记忆分析原因。"}
        tabs = []
        for sess in driver.get_all_sessions(): 
            sess.pop('connected_at', None)
            sess.pop('type', None)
            sess['url'] = sess.get('url', '')[:50] + ("..." if len(sess.get('url', '')) > 50 else "")
            tabs.append(sess)
        if switch_tab_id: driver.default_session_id = switch_tab_id
        result = {
            "status": "success",
            "metadata": {
                "tabs_count": len(tabs), "tabs": tabs,
                "active_tab": driver.default_session_id
            }
        }
        if not tabs_only: 
            importlib.reload(simphtml); result["content"] = simphtml.get_html(driver, cutlist=True, maxchars=35000, text_only=text_only)
            if text_only: result['content'] = smart_format(result['content'], max_str_len=10000, omit_str='\n\n[omitted long content]\n\n')
        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}
    
def format_error(e):
    exc_type, exc_value, exc_traceback = sys.exc_info()
    tb = traceback.extract_tb(exc_traceback)
    if tb:
        f = tb[-1]
        fname = os.path.basename(f.filename)
        return f"{exc_type.__name__}: {str(e)} @ {fname}:{f.lineno}, {f.name} -> `{f.line}`"
    return f"{exc_type.__name__}: {str(e)}"

# 保护 log_memory_access 的 stats 文件免受并发写入损坏
_memory_stats_lock = threading.Lock()
# os.chdir 是进程级状态——inline_eval 内的 chdir/restore 必须串行化，
# 否则多线程 / 多 agent 并发会互相串 cwd
_inline_eval_cwd_lock = threading.Lock()

def log_memory_access(path):
    if 'memory' not in path: return
    script_dir = os.path.dirname(os.path.abspath(__file__))
    stats_file = os.path.join(script_dir, 'memory/file_access_stats.json')
    with _memory_stats_lock:
        try:
            with open(stats_file, 'r', encoding='utf-8') as f: stats = json.load(f)
        except Exception: stats = {}
        fname = os.path.basename(path)
        stats[fname] = {'count': stats.get(fname, {}).get('count', 0) + 1, 'last': datetime.now().strftime('%Y-%m-%d')}
        # 原子写：先写临时文件再 rename，避免崩溃中断造成 stats 文件损坏
        tmp = stats_file + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f: json.dump(stats, f, indent=2, ensure_ascii=False)
            os.replace(tmp, stats_file)
        except Exception:
            try: os.path.exists(tmp) and os.remove(tmp)
            except Exception: pass

def web_execute_js(script, switch_tab_id=None, no_monitor=False):
    """执行 JS 脚本来控制浏览器，并捕获结果和页面变化"""
    global driver
    try:
        if driver is None: first_init_driver()
        if len(driver.get_all_sessions()) == 0: return {"status": "error", "msg": "没有可用的浏览器标签页，查L3记忆分析原因。"}
        if switch_tab_id: driver.default_session_id = switch_tab_id
        result = simphtml.execute_js_rich(script, driver, no_monitor=no_monitor)
        return result
    except Exception as e: return {"status": "error", "msg": format_error(e)}

def expand_file_refs(text, base_dir=None):
    """展开文本中的 {{file:路径:起始行:结束行}} 引用为实际文件内容。
    可与普通文本混排。展开失败抛 ValueError。
    base_dir: 相对路径的基准目录，默认为进程 cwd"""
    pattern = r'\{\{file:(.+?):(\d+):(\d+)\}\}'
    def replacer(match):
        path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        path = os.path.abspath(os.path.join(base_dir or '.', path))
        if not os.path.isfile(path): raise ValueError(f"引用文件不存在: {path}")
        with open(path, 'r', encoding='utf-8') as f: lines = f.readlines()
        if start < 1 or end > len(lines) or start > end: raise ValueError(f"行号越界: {path} 共{len(lines)}行, 请求{start}-{end}")
        return ''.join(lines[start-1:end])
    return re.sub(pattern, replacer, text)
    
def file_patch(path: str, old_content: str, new_content: str):
    """在文件中寻找唯一的 old_content 块并替换为 new_content"""
    path = str(Path(path).resolve())
    try:
        if not os.path.exists(path): return {"status": "error", "msg": "文件不存在"}
        with open(path, 'r', encoding='utf-8') as f: full_text = f.read()
        if not old_content: return {"status": "error", "msg": "old_content 为空，请确认 arguments"}
        count = full_text.count(old_content)
        if count == 0: return {"status": "error", "msg": "未找到匹配的旧文本块，建议：先用 file_read 确认当前内容，再分小段进行 patch。若多次失败则询问用户，严禁自行使用 overwrite 或代码替换。"}
        if count > 1: return {"status": "error", "msg": f"找到 {count} 处匹配，无法确定唯一位置。请提供更长、更具体的旧文本块以确保唯一性。建议：包含上下文行来增强特征，或分小段逐个修改。"}
        updated_text = full_text.replace(old_content, new_content)
        with open(path, 'w', encoding='utf-8') as f: f.write(updated_text)
        return {"status": "success", "msg": "文件局部修改成功"}
    except Exception as e: return {"status": "error", "msg": str(e)}

_read_dirs = collections.OrderedDict()  # 有界 LRU：path -> None；防止长跑 agent 无限累积
_READ_DIRS_MAX = 200

# file_read 输出预算：单行最大长度按行数动态分配——总字符配额 / 行数，
# 但每行最少 _LINE_MIN、最多 _LINE_MAX 个字符，避免极端值。
# 行数尾部探测上限 _TAIL_PROBE：file_read 在主 slice 之后再扫这么多行用于
# 给出"文件还有多少行"的提示；超过阈值显示 "<n>+"。
_FILE_READ_BUDGET = 256_000
_FILE_READ_LINE_MIN = 100
_FILE_READ_LINE_MAX = 8_000
_FILE_READ_TAIL_PROBE = 5_000
_FILE_READ_DEFAULT_COUNT = 200
def _record_read_dir(path):
    p = os.path.dirname(os.path.abspath(path))
    _read_dirs.pop(p, None)
    _read_dirs[p] = None
    while len(_read_dirs) > _READ_DIRS_MAX:
        _read_dirs.popitem(last=False)
def _scan_files(base, depth=2):
    try:
        for e in os.scandir(base):
            if e.is_file(): yield (e.name, e.path)
            elif depth > 0 and e.is_dir(follow_symlinks=False): yield from _scan_files(e.path, depth - 1)
    except (PermissionError, OSError): pass
def file_read(path, start=1, keyword=None, count=_FILE_READ_DEFAULT_COUNT, show_linenos=True):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            stream = ((i, l.rstrip('\r\n')) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)
            if keyword:
                before = collections.deque(maxlen=count//3)
                for i, l in stream:
                    if keyword.lower() in l.lower():
                        res = list(before) + [(i, l)] + list(itertools.islice(stream, count - len(before) - 1))
                        break
                    before.append((i, l))
                else: return f"Keyword '{keyword}' not found after line {start}. Falling back to content from line {start}:\n\n" \
                               + file_read(path, start, None, count, show_linenos)
            else: res = list(itertools.islice(stream, count))
            realcnt = len(res)
            L_MAX = min(max(_FILE_READ_LINE_MIN, _FILE_READ_BUDGET // max(realcnt, 1)), _FILE_READ_LINE_MAX)
            TAG = " ... [TRUNCATED]"
            remaining = sum(1 for _ in itertools.islice(stream, _FILE_READ_TAIL_PROBE))
            total_lines = (res[0][0] - 1 if res else start - 1) + realcnt + remaining
            tl_str = f"{total_lines}+" if remaining >= _FILE_READ_TAIL_PROBE else str(total_lines)
            partial = total_lines > realcnt
            total_tag = f"[FILE] {tl_str} lines" + (f" | PARTIAL showing {realcnt}; assess need for more" if partial else "") + "\n"
            res = [(i, l if len(l) <= L_MAX else l[:L_MAX] + TAG) for i, l in res]
            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)
            if show_linenos: result = total_tag + result
            elif partial: result += f"\n\n[FILE PARTIAL: showing {realcnt}/{tl_str} lines; assess need for more]"
            _record_read_dir(path)
            return result
    except FileNotFoundError:
        msg = f"Error: File not found: {path}"
        try:
            tgt = os.path.basename(path); scan = os.path.dirname(os.path.dirname(os.path.abspath(path)))
            roots = [scan] + [d for d in _read_dirs if not d.startswith(scan)]
            cands = list(itertools.islice((c for base in roots for c in _scan_files(base)), 2000))
            top = sorted([(difflib.SequenceMatcher(None, tgt.lower(), c[0].lower()).ratio(), c) for c in cands[:2000]], key=lambda x: -x[0])[:5]
            top = [(s, c) for s, c in top if s > 0.3]
            if top: msg += "\n\nDid you mean:\n" + "\n".join(f"  {c[1]}  ({s:.0%})" for s, c in top)
        except Exception: pass
        return msg
    except Exception as e: return f"Error: {str(e)}"

def smart_format(data, max_str_len=100, omit_str=' ... '):
    if not isinstance(data, str): data = str(data)
    if len(data) < max_str_len + len(omit_str)*2: return data
    return f"{data[:max_str_len//2]}{omit_str}{data[-max_str_len//2:]}"

def consume_file(dr, file):
    if dr and os.path.exists(os.path.join(dr, file)): 
        with open(os.path.join(dr, file), encoding='utf-8', errors='replace') as f: content = f.read()
        os.remove(os.path.join(dr, file))
        return content

class WlwlAssHandler(BaseHandler):
    '''wlwl-ass 工具库，包含多种工具的实现。工具函数自动加上了 do_ 前缀。实际工具名没有前缀。'''
    def __init__(self, parent, last_history=None, cwd='./temp', permission_policy=None, permission_prompter=None, project_root=None):
        self.parent = parent
        self.working = {}
        self.cwd = cwd;  self.current_turn = 0
        self.project_root = project_root
        self.permission_policy = permission_policy
        self.permission_prompter = permission_prompter
        self.history_info = last_history if last_history else []
        self.code_stop_signal = []
        self._done_hooks = []

    def dispatch(self, tool_name, args, response, index=0):
        metadata = tool_metadata(tool_name)
        request = ToolPermissionRequest(tool_name, args, self.cwd, self.project_root, metadata)
        if self.permission_policy:
            decision = self.permission_policy.decide(request)
            if decision.decision == PermissionDecision.ASK and self.permission_prompter:
                decision = self.permission_prompter.ask(request, decision.message)
            if decision.decision != PermissionDecision.ALLOW:
                msg = decision.message or f"Permission denied: {metadata.display_name}"
                yield f"[Permission] {msg}\n"
                return StepOutcome({"status": "error", "msg": msg}, next_prompt="\n")
        start_t = time.monotonic()
        if tool_name != 'no_tool':
            activity_log.record({
                'phase': 'tool_start',
                'turn': self.current_turn,
                'tool': tool_name,
                'args': activity_log.compact_args(tool_name, args),
            })
        try:
            outcome = yield from super().dispatch(tool_name, args, response, index=index)
            return outcome
        finally:
            elapsed = time.monotonic() - start_t
            if tool_name != 'no_tool':
                # 抽取 status 用于失败率统计；data 可能不是 dict（例如 StepOutcome(response, ...)
                # 在 no_tool 路径），尽量提取，提取不到记 'unknown'。
                status = 'unknown'
                try:
                    _data = locals().get('outcome', None)
                    if _data is not None and isinstance(getattr(_data, 'data', None), dict):
                        status = str(_data.data.get('status', 'success')) or 'success'
                    elif _data is not None:
                        status = 'success'  # 工具返回非 dict 视为成功
                except Exception:
                    pass
                activity_log.record({
                    'phase': 'tool_end',
                    'turn': self.current_turn,
                    'tool': tool_name,
                    'elapsed_s': round(elapsed, 3),
                    'status': status,
                })
                yield f"[Tool completed in {elapsed:.1f}s]\n"

    def _get_abs_path(self, path):
        if not path: return ""
        return os.path.abspath(os.path.join(self.cwd, path))   

    def _extract_code_block(self, response, code_type):
        """从模型回复中提取最后一个匹配的代码块。

        取 ``matches[-1]`` 而不是 ``[0]`` 是有意为之：模型常先在 <thinking> 或正文中
        给"草稿/反例代码块"，然后在末尾给最终要执行的代码块。取最后一个使
        "最终要执行的版本"覆盖前面的草稿，符合 chain-of-thought 写作习惯。
        """
        code_type = {'python':'python|py', 'powershell':'powershell|ps1|pwsh', 'bash':'bash|sh|shell'}.get(code_type, re.escape(code_type))
        matches = re.findall(rf"```(?:{code_type})\n(.*?)\n```", response.content, re.DOTALL)
        return matches[-1].strip() if matches else None

    def do_code_run(self, args, response):
        '''执行代码片段，有长度限制，不允许代码中放大量数据，如有需要应当通过文件读取进行。'''
        code_type = args.get("type", "python")
        code = args.get("code") or args.get("script")
        if not code:
            code = self._extract_code_block(response, code_type)
            if not code: return StepOutcome("[Error] Code missing. Must use reply code block or 'script' arg.", next_prompt="\n")
        timeout = args.get("timeout", 60)
        raw_path = os.path.join(self.cwd, args.get("cwd", './'))
        cwd = os.path.normpath(os.path.abspath(raw_path))
        code_cwd = os.path.normpath(self.cwd)
        if code_type == 'python' and args.get("inline_eval"):
            ns = {'handler': self, 'parent': self.parent}
            # 加锁串行化 chdir，避免并发 inline_eval 互相串 cwd（os.chdir 进程级）
            with _inline_eval_cwd_lock:
                old_cwd = os.getcwd()
                try:
                    os.chdir(cwd)
                    try:
                        try: result = repr(eval(code, ns))
                        except SyntaxError: exec(code, ns); result = ns.get('_r', 'OK')
                    except Exception as e: result = f'Error: {e}'
                finally: os.chdir(old_cwd)
        else: result = yield from code_run(code, code_type, timeout, cwd, code_cwd=code_cwd, stop_signal=self.code_stop_signal)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_ask_user(self, args, response):
        question = args.get("question", "请提供输入：")
        candidates = args.get("candidates", [])
        result = ask_user(question, candidates)
        yield f"Waiting for your answer ...\n"
        return StepOutcome(result, next_prompt="", should_exit=True)
    
    def do_web_scan(self, args, response):
        '''获取当前页面内容和标签页列表。也可用于切换标签页。
        注意：HTML经过简化，边栏/浮动元素等可能被过滤。如需查看被过滤的内容请用execute_js。
        tabs_only=true时仅返回标签页列表，不获取HTML（省token）'''
        tabs_only = args.get("tabs_only", False)
        switch_tab_id = args.get("switch_tab_id", None)
        text_only = args.get("text_only", False)
        result = web_scan(tabs_only=tabs_only, switch_tab_id=switch_tab_id, text_only=text_only)
        content = result.pop("content", None)
        yield f'[Info] {str(result)}\n'
        if content: result = json.dumps(result, ensure_ascii=False, default=json_default) + f"\n```html\n{content}\n```"
        next_prompt = "\n"
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_web_execute_js(self, args, response):
        '''web情况下的优先使用工具，执行任何js达成对浏览器的*完全*控制。支持将结果保存到文件供后续读取分析。'''
        script = args.get("script", "") or self._extract_code_block(response, "javascript")
        if not script: return StepOutcome("[Error] Script missing. Use ```javascript block or 'script' arg.", next_prompt="\n")
        # 兼容"script 实际是 .js 文件路径"的写法，但严格收紧条件，
        # 避免把 'document.title' 这种正常 JS 当成路径误读到任意文件：
        # 必须单行、长度 < 512、扩展名是 .js / .javascript。
        stripped = script.strip()
        looks_like_path = (
            '\n' not in stripped
            and len(stripped) < 512
            and stripped.lower().endswith(('.js', '.javascript'))
        )
        if looks_like_path:
            abs_path = self._get_abs_path(stripped)
            if os.path.isfile(abs_path):
                with open(abs_path, 'r', encoding='utf-8') as f: script = f.read()
        save_to_file = args.get("save_to_file", "")
        switch_tab_id = args.get("switch_tab_id") or args.get("tab_id")
        no_monitor = args.get("no_monitor", False)
        result = web_execute_js(script, switch_tab_id=switch_tab_id, no_monitor=no_monitor)
        if save_to_file and "js_return" in result:
            content = str(result["js_return"] or '')
            abs_path = self._get_abs_path(save_to_file)
            result["js_return"] = smart_format(content, max_str_len=170)
            try:
                with open(abs_path, 'w', encoding='utf-8') as f: f.write(str(content))
                result["js_return"] += f"\n\n[已保存完整内容到 {abs_path}]"
            except OSError as _save_err:
                result['js_return'] += f"\n\n[保存失败，无法写入文件 {abs_path}: {_save_err}]"
        show = smart_format(json.dumps(result, ensure_ascii=False, indent=2, default=json_default), max_str_len=300)
        try: print("Web Execute JS Result:", show)
        except (UnicodeEncodeError, OSError): pass
        yield f"JS 执行结果:\n{show}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        result = json.dumps(result, ensure_ascii=False, default=json_default)
        return StepOutcome(smart_format(result, max_str_len=8000), next_prompt=next_prompt)
    
    def do_file_patch(self, args, response):
        path = self._get_abs_path(args.get("path", ""))
        yield f"[Action] Patching file: {path}\n"
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")
        try: new_content = expand_file_refs(new_content, base_dir=self.cwd)
        except ValueError as e:
            yield f"[Status] ❌ 引用展开失败: {e}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        result = file_patch(path, old_content, new_content)
        yield f"\n{str(result)}\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)
    
    def do_file_write(self, args, response):
        '''用于对整个文件的大量处理，精细修改要用file_patch。
        需要将要写入的内容放在<file_content>标签内，或者放在代码块中'''
        path = self._get_abs_path(args.get("path", ""))
        mode = args.get("mode", "overwrite")  # overwrite/append/prepend
        action_str = {"prepend": "Prepending to", "append": "Appending to"}.get(mode, "Overwriting")
        yield f"[Action] {action_str} file: {os.path.basename(path)}\n"

        def extract_robust_content(text):
            tag = re.search(r"<file_content[^>]*>(.*)</file_content>", text, re.DOTALL)
            if tag: return tag.group(1).strip()
            s, e = text.find("```"), text.rfind("```")
            if -1 < s < e: return text[text.find("\n", s)+1 : e].strip()
            return None
        
        blocks = extract_robust_content(response.content)
        if not blocks:
            yield f"[Status] ❌ 失败: 未在回复中找到<file_content>代码块内容\n"
            return StepOutcome({"status": "error", "msg": "No content found. Put content inside <file_content>...</file_content> tags in your reply body before call file_write."}, next_prompt="\n")
        try:
            new_content = expand_file_refs(blocks, base_dir=self.cwd)
            if mode == "prepend":
                old = ""
                if os.path.exists(path):
                    with open(path, 'r', encoding="utf-8") as _rf: old = _rf.read()
                with open(path, 'w', encoding="utf-8") as _wf: _wf.write(new_content + old)
            else:
                with open(path, 'a' if mode == "append" else 'w', encoding="utf-8") as f: f.write(new_content)
            yield f"[Status] ✅ {mode.capitalize()} 成功 ({len(new_content)} bytes)\n"
            next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
            return StepOutcome({"status": "success", 'writed_bytes': len(new_content)}, next_prompt=next_prompt)
        except Exception as e:
            yield f"[Status] ❌ 写入异常: {str(e)}\n"
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="\n")
        
    def do_file_read(self, args, response):
        '''读取文件内容。从第start行开始读取。如有keyword则返回第一个keyword(忽略大小写)周边内容'''
        path = self._get_abs_path(args.get("path", ""))
        yield f"\n[Action] Reading file: {path}\n"
        start = args.get("start", 1)
        count = args.get("count", _FILE_READ_DEFAULT_COUNT)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)
        result = file_read(path, start=start, keyword=keyword,
                           count=count, show_linenos=show_linenos)
        if show_linenos and not result.startswith("Error:"): result = '由于设置了show_linenos，以下返回信息为：(行号|)内容 。\n' + result 
        if ' ... [TRUNCATED]' in result: result += '\n\n（某些行被截断，如需完整内容可改用 code_run 读取）'
        result = smart_format(result, max_str_len=20000, omit_str='\n\n[omitted long content]\n\n')
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        log_memory_access(path)
        if 'memory' in path or 'sop' in path: 
            next_prompt += "\n[SYSTEM TIPS] 正在读取记忆或SOP文件，若决定按sop执行请提取sop中的关键点（特别是靠后的）update working memory."
        return StepOutcome(result, next_prompt=next_prompt)
    
    def _in_plan_mode(self): return self.working.get('in_plan_mode')
    def _exit_plan_mode(self): self.working.pop('in_plan_mode', None)
    def enter_plan_mode(self, plan_path): 
        self.working['in_plan_mode'] = plan_path; self.max_turns = 100
        print(f"[Info] Entered plan mode with plan file: {plan_path}"); return plan_path
    def _check_plan_completion(self):
        if not os.path.isfile(p:=self._in_plan_mode() or ''): return None
        try:
            with open(p, encoding='utf-8', errors='replace') as f: return len(re.findall(r'\[ \]', f.read()))
        except Exception: return None
    
    def do_update_working_checkpoint(self, args, response):
        '''为整个任务设定后续需要临时记忆的重点。'''
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")
        if "key_info" in args: self.working['key_info'] = key_info
        if "related_sop" in args: self.working['related_sop'] = related_sop
        self.working['passed_sessions'] = 0
        yield f"[Info] Updated key_info and related_sop.\n"
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        #next_prompt += '\n[SYSTEM TIPS] 此函数一般在任务开始或中间时调用，如果任务已成功完成应该是start_long_term_update用于结算长期记忆。\n'
        return StepOutcome({"result": "working key_info updated"}, next_prompt=next_prompt)

    def do_sop_search(self, args, response):
        '''在 Sophub 检索别人分享的 SOP / skill。'''
        from tools.sop_tools import sop_search
        query = args.get('query', '') or ''
        top_k = args.get('top_k', 5)
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = 5
        result = sop_search(query, top_k=top_k)
        yield f"[sop_search] {query!r} (top {top_k})\n"
        return StepOutcome(result, next_prompt="\n")

    def do_sop_read(self, args, response):
        '''按 id 拉取 Sophub SOP 完整内容。应用前必须先 read。'''
        from tools.sop_tools import sop_read
        sop_id = args.get('sop_id', '') or ''
        result = sop_read(sop_id)
        yield f"[sop_read] {sop_id}\n"
        return StepOutcome(result, next_prompt="\n")

    def do_web_search(self, args, response):
        '''Grok 原生 live_search + Tavily REST 并发联网搜索。详见 memory/web_search_sop.md。'''
        from tools.web_search import web_search
        query = (args.get('query') or '').strip()
        if not query:
            return StepOutcome("[web_search error] query is required", next_prompt="\n")
        try:
            depth = str(args.get('depth') or 'basic').lower()
            max_results = int(args.get('max_results') or 5)
            sources = args.get('sources')
            if isinstance(sources, str):
                sources = [s.strip() for s in sources.split(',') if s.strip()]
            result = web_search(query, sources=sources, max_results=max_results, depth=depth)
        except Exception as e:
            return StepOutcome(f"[web_search error] {format_error(e)}", next_prompt="\n")
        yield f"[web_search] {query!r} (depth={depth})\n"
        return StepOutcome(result, next_prompt="\n")

    def do_wechat_send(self, args, response):
        '''wxauto 驱动 PC 微信外发。失败请 sop_read wechat_ljqctrl_sop 走坐标回退。'''
        from tools.wechat import wechat_send
        to = (args.get('to') or '').strip()
        text = (args.get('text') or '').strip()
        files = args.get('files')
        if not to or not text:
            return StepOutcome("[wechat_send error] to + text required", next_prompt="\n")
        if not isinstance(files, list):
            files = None
        try:
            result = wechat_send(to, text, files=files)
        except Exception as e:
            return StepOutcome(f"[wechat_send error] {format_error(e)}", next_prompt="\n")
        yield f"[wechat_send] {to!r} ({len(text)} chars)\n"
        return StepOutcome(result, next_prompt="\n")

    def do_mcp_call(self, args, response):
        '''单一 MCP 工具入口：列出 server / 列出工具 / 实际调用，三种语义共享同一工具。'''
        from tools.mcp_client import mcp_call
        server = (args.get('server') or '').strip() or None
        tool = (args.get('tool') or '').strip() or None
        arguments = args.get('arguments') or None
        if arguments is not None and not isinstance(arguments, dict):
            # Some models pass arguments as a JSON-encoded string. Best-effort
            # decode rather than dropping the call on the floor.
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                arguments = {"_raw": str(arguments)}
        timeout = args.get('timeout')
        try:
            timeout = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout = None
        if server and tool:
            yield f"[mcp_call] {server}/{tool}\n"
        elif server:
            yield f"[mcp_call] list tools on {server}\n"
        else:
            yield "[mcp_call] list servers\n"
        result = mcp_call(server=server, tool=tool, arguments=arguments, timeout=timeout)
        return StepOutcome(result, next_prompt="\n")

    def do_process(self, args, response):
        '''Background-process registry: register / list / kill / cleanup_dead.'''
        from launcher.process_registry import get_registry
        action = (args.get('action') or 'list').strip()
        reg = get_registry()
        if action == 'register':
            label = (args.get('label') or '').strip()
            pid = args.get('pid')
            if not label or not isinstance(pid, int):
                yield "[process] register requires label + pid (int)\n"
                return StepOutcome({"error": "missing_field"}, next_prompt="\n")
            entry = reg.register(label, int(pid),
                                 kind=str(args.get('kind') or 'proc'),
                                 cmd=args.get('cmd'))
            yield f"[process] registered {label!r} pid={pid}\n"
            return StepOutcome({"entry": entry}, next_prompt="\n")
        if action == 'list':
            rows = reg.list()
            yield f"[process] {len(rows)} entries\n"
            return StepOutcome({"processes": rows}, next_prompt="\n")
        if action == 'kill':
            target = args.get('pid', args.get('label'))
            if target is None:
                yield "[process] kill requires pid or label\n"
                return StepOutcome({"error": "missing_field"}, next_prompt="\n")
            ok, msg = reg.kill(target, force=bool(args.get('force')))
            yield f"[process] kill {target}: {msg}\n"
            return StepOutcome({"ok": ok, "message": msg}, next_prompt="\n")
        if action == 'cleanup_dead':
            n = reg.cleanup_dead()
            yield f"[process] cleaned {n} dead entries\n"
            return StepOutcome({"removed": n}, next_prompt="\n")
        return StepOutcome({"error": f"unknown action {action!r}"}, next_prompt="\n")

    def do_checkpoint(self, args, response):
        '''Persistent checkpoint store for long tasks: save / load / list / clear / list_tasks.'''
        from launcher.checkpoint_manager import get_manager
        action = (args.get('action') or 'list_tasks').strip()
        cm = get_manager()
        if action == 'list_tasks':
            tasks = cm.list_tasks()
            yield f"[checkpoint] {len(tasks)} task(s)\n"
            return StepOutcome({"tasks": tasks}, next_prompt="\n")
        task_id = (args.get('task_id') or '').strip()
        if not task_id and action != 'list_tasks':
            yield f"[checkpoint] {action} requires task_id\n"
            return StepOutcome({"error": "missing_task_id"}, next_prompt="\n")
        if action == 'save':
            state = args.get('state')
            if state is None:
                yield "[checkpoint] save requires state\n"
                return StepOutcome({"error": "missing_state"}, next_prompt="\n")
            meta = cm.save(task_id, state, note=str(args.get('note') or ''))
            yield f"[checkpoint] saved {meta['id']} for task {task_id}\n"
            return StepOutcome({"checkpoint": meta}, next_prompt="\n")
        if action == 'load':
            cp = cm.load(task_id, checkpoint_id=args.get('checkpoint_id'))
            if cp is None:
                yield f"[checkpoint] no checkpoint for {task_id}\n"
                return StepOutcome({"error": "not_found"}, next_prompt="\n")
            yield f"[checkpoint] loaded {cp.get('id')} for {task_id}\n"
            return StepOutcome({"checkpoint": cp}, next_prompt="\n")
        if action == 'list':
            rows = cm.list_checkpoints(task_id)
            yield f"[checkpoint] {len(rows)} checkpoint(s) for {task_id}\n"
            return StepOutcome({"checkpoints": rows}, next_prompt="\n")
        if action == 'clear':
            n = cm.clear(task_id)
            yield f"[checkpoint] cleared {n} from {task_id}\n"
            return StepOutcome({"removed": n}, next_prompt="\n")
        return StepOutcome({"error": f"unknown action {action!r}"}, next_prompt="\n")

    def do_vision(self, args, response):
        '''Image understanding: describe / ocr / extract.'''
        from tools import vision_tools
        action = (args.get('action') or 'describe').strip()
        image_path = (args.get('image_path') or '').strip()
        if not image_path:
            yield "[vision] image_path is required\n"
            return StepOutcome({"error": "missing_field"}, next_prompt="\n")
        prompt = args.get('prompt')
        if action == 'describe':
            text = vision_tools.describe_image(image_path, prompt=prompt)
            yield f"[vision] describe {image_path!r}\n"
            return StepOutcome({"text": text}, next_prompt="\n")
        if action == 'ocr':
            text = vision_tools.ocr_image(image_path, lang=str(args.get('lang') or 'auto'))
            yield f"[vision] ocr {image_path!r}\n"
            return StepOutcome({"text": text}, next_prompt="\n")
        if action == 'extract':
            schema = args.get('schema')
            if not isinstance(schema, (dict, str)):
                yield "[vision] extract requires schema (dict or string)\n"
                return StepOutcome({"error": "missing_schema"}, next_prompt="\n")
            data = vision_tools.extract_structured(image_path, schema, prompt=prompt)
            yield f"[vision] extract {image_path!r}\n"
            return StepOutcome({"data": data}, next_prompt="\n")
        return StepOutcome({"error": f"unknown action {action!r}"}, next_prompt="\n")

    def do_gui_operator(self, args, response):
        '''Desktop GUI operator: observe / parse / act / run.'''
        from tools import gui_operator
        action = (args.get('action') or 'observe').strip()
        if action == 'observe':
            result = gui_operator.observe_desktop(
                output_path=args.get('output_path'),
                include_base64=_bool_arg(args.get('include_base64'), False),
                all_screens=_bool_arg(args.get('all_screens'), False),
            )
            if result.get("status") == "success":
                yield f"[gui_operator] screenshot -> {result.get('path')}\n"
            else:
                yield f"[gui_operator] observe error: {result.get('detail') or result.get('error')}\n"
            return StepOutcome(result, next_prompt="\n")
        if action == 'parse':
            prediction = str(args.get('prediction') or args.get('action_text') or '')
            if not prediction:
                yield "[gui_operator] parse requires prediction/action_text\n"
                return StepOutcome({"error": "missing_prediction"}, next_prompt="\n")
            try:
                screen_width = int(args.get('screen_width') or 1000)
                screen_height = int(args.get('screen_height') or 1000)
                scale_factor = float(args.get('scale_factor') or 1.0)
                parsed = gui_operator.parse_actions(
                    prediction,
                    screen_width=screen_width,
                    screen_height=screen_height,
                    scale_factor=scale_factor,
                )
            except Exception as exc:
                yield f"[gui_operator] parse error: {exc}\n"
                return StepOutcome({"error": str(exc)}, next_prompt="\n")
            data = [gui_operator.parsed_action_to_dict(p) for p in parsed]
            yield f"[gui_operator] parsed {len(data)} action(s)\n"
            return StepOutcome({"actions": data}, next_prompt="\n")
        if action == 'act':
            action_text = str(args.get('action_text') or '')
            if not action_text:
                yield "[gui_operator] act requires action_text\n"
                return StepOutcome({"error": "missing_action_text"}, next_prompt="\n")
            try:
                result = gui_operator.execute_desktop_action(
                    action_text,
                    screen_width=args.get('screen_width'),
                    screen_height=args.get('screen_height'),
                    scale_factor=float(args.get('scale_factor') or 1.0),
                    dry_run=_bool_arg(args.get('dry_run'), True),
                )
            except Exception as exc:
                yield f"[gui_operator] act error: {exc}\n"
                return StepOutcome({"error": str(exc)}, next_prompt="\n")
            yield f"[gui_operator] {result.get('status')} {action_text}\n"
            return StepOutcome(result, next_prompt="\n")
        if action == 'run':
            instruction = str(args.get('instruction') or args.get('task') or '').strip()
            if not instruction:
                yield "[gui_operator] run requires instruction\n"
                return StepOutcome({"error": "missing_instruction"}, next_prompt="\n")
            try:
                result = gui_operator.run_visual_task(
                    instruction,
                    max_loop=int(args.get('max_loop') or 5),
                    loop_wait=float(args.get('loop_wait') or 1.0),
                    dry_run=_bool_arg(args.get('dry_run'), True),
                    include_base64=_bool_arg(args.get('include_base64'), False),
                    all_screens=_bool_arg(args.get('all_screens'), False),
                    backend=str(args.get('backend') or 'auto'),
                )
            except Exception as exc:
                yield f"[gui_operator] run error: {exc}\n"
                return StepOutcome({"error": str(exc)}, next_prompt="\n")
            yield f"[gui_operator] run {result.get('status')} steps={len(result.get('steps') or [])} dir={result.get('run_dir')}\n"
            return StepOutcome(result, next_prompt="\n")
        return StepOutcome({"error": f"unknown action {action!r}"}, next_prompt="\n")

    def do_browser_operator(self, args, response):
        '''Hybrid browser operator: DOM/JS first, visual fallback.'''
        from tools import browser_hybrid_operator, gui_operator

        instruction = str(args.get('instruction') or args.get('task') or '').strip()
        if not instruction:
            yield "[browser_operator] instruction is required\n"
            return StepOutcome({"error": "missing_instruction"}, next_prompt="\n")
        try:
            result = browser_hybrid_operator.run_browser_task(
                instruction,
                scan_func=web_scan,
                visual_run_func=gui_operator.run_visual_task,
                force_visual=_bool_arg(args.get('force_visual'), False),
                dry_run=_bool_arg(args.get('dry_run'), True),
                max_loop=int(args.get('max_loop') or 5),
                loop_wait=float(args.get('loop_wait') or 1.0),
                backend=str(args.get('backend') or 'auto'),
            )
        except Exception as exc:
            yield f"[browser_operator] error: {exc}\n"
            return StepOutcome({"error": str(exc)}, next_prompt="\n")
        yield f"[browser_operator] {result.get('strategy')} {result.get('status')}\n"
        return StepOutcome(result, next_prompt="\n")

    def do_forum_harvest(self, args, response):
        '''Harvest free-API posts from the user's logged-in linux.do session.'''
        import json as _json
        from tools import forum_harvester
        from launcher import free_pool_vault

        max_topics = int(args.get('max_topics') or 20)
        require_keywords = _bool_arg(args.get('require_keywords'), True)
        auto_probe = _bool_arg(args.get('auto_probe'), False)

        latest_script = (
            "return fetch('/latest.json', {credentials: 'include'})"
            ".then(r => r.text()).then(t => t);"
        )
        latest_resp = web_execute_js(latest_script)
        latest_text = _harvest_extract_text(latest_resp)
        if not latest_text:
            yield "[forum_harvest] failed to fetch /latest.json (是否已登录 linux.do？)\n"
            return StepOutcome({"error": "latest_fetch_failed"}, next_prompt="\n")
        try:
            latest_json = _json.loads(latest_text)
        except _json.JSONDecodeError as exc:
            yield f"[forum_harvest] /latest.json not JSON: {exc}\n"
            return StepOutcome({"error": "latest_not_json"}, next_prompt="\n")

        topic_ids = forum_harvester.select_topic_ids_from_latest(
            latest_json, limit=max_topics, require_keywords=require_keywords,
        )
        yield f"[forum_harvest] picked {len(topic_ids)} topics from latest\n"

        new_candidates: list[dict] = []
        for tid in topic_ids:
            script = (
                f"return fetch('/t/{int(tid)}.json', {{credentials: 'include'}})"
                ".then(r => r.text());"
            )
            topic_resp = web_execute_js(script)
            topic_text = _harvest_extract_text(topic_resp)
            if not topic_text:
                continue
            try:
                topic_json = _json.loads(topic_text)
            except _json.JSONDecodeError:
                continue
            cands = forum_harvester.extract_from_topic_json(topic_json)
            for c in cands:
                payload = c.to_dict()
                free_pool_vault.append_candidate(payload)
                try:
                    entry = free_pool_vault.add_or_update(
                        base_url=c.base_url,
                        key=c.key,
                        claimed_model=c.claimed_model,
                        source=payload.get("source"),
                    )
                except ValueError:
                    continue
                if entry.get("status") == "candidate":
                    new_candidates.append({"entry_id": entry["id"], **payload})

        yield f"[forum_harvest] {len(new_candidates)} new candidates queued\n"

        probe_results: list[dict] = []
        if auto_probe and new_candidates:
            from tools import api_probe
            for cand in new_candidates[:max_topics]:
                try:
                    result = api_probe.probe_endpoint(
                        base_url=cand["base_url"],
                        key=cand["key"],
                        claimed_model=cand.get("claimed_model") or "",
                        source=cand.get("source"),
                    )
                except Exception as exc:
                    yield f"[forum_harvest] probe error on {cand['entry_id']}: {exc}\n"
                    continue
                probe_results.append({
                    "entry_id": result.entry_id,
                    "ok": result.ok,
                    "score": result.weighted_average,
                    "guess": result.actual_model_guess,
                    "matches": result.matches_claim,
                })

        return StepOutcome(
            {
                "topic_ids": topic_ids,
                "new_candidates": new_candidates,
                "probe_results": probe_results,
            },
            next_prompt="\n",
        )

    def do_api_probe(self, args, response):
        '''Probe / sweep / archive entries in the free-pool vault.'''
        from launcher import free_pool_vault
        from tools import api_probe

        action = (args.get('action') or 'list').strip()
        if action == 'list':
            entries = free_pool_vault.list_all()
            yield f"[api_probe] vault has {len(entries)} entries\n"
            return StepOutcome(
                {"entries": [
                    {
                        "id": e.get("id"),
                        "base_url": e.get("base_url"),
                        "claimed_model": e.get("claimed_model"),
                        "status": e.get("status"),
                        "score": (e.get("fingerprint") or {}).get("weighted_average"),
                        "actual_model_guess": (e.get("fingerprint") or {}).get("actual_model_guess"),
                        "expires_at": e.get("expires_at"),
                    } for e in entries
                ]},
                next_prompt="\n",
            )
        if action == 'sweep':
            archived = free_pool_vault.sweep_expired()
            yield f"[api_probe] swept {len(archived)} expired entries\n"
            return StepOutcome({"archived": archived}, next_prompt="\n")
        if action == 'archive':
            entry_id = (args.get('entry_id') or '').strip()
            reason = (args.get('reason') or 'manual').strip()
            if not entry_id:
                yield "[api_probe] archive requires entry_id\n"
                return StepOutcome({"error": "missing_entry_id"}, next_prompt="\n")
            ok = free_pool_vault.archive(entry_id, reason=reason)
            yield f"[api_probe] archive {entry_id} -> {ok}\n"
            return StepOutcome({"archived": ok, "entry_id": entry_id}, next_prompt="\n")
        if action == 'probe_one':
            base_url = (args.get('base_url') or '').strip()
            key = (args.get('key') or '').strip()
            claimed = (args.get('claimed_model') or '').strip()
            if not base_url or not key:
                yield "[api_probe] probe_one requires base_url + key\n"
                return StepOutcome({"error": "missing_base_url_or_key"}, next_prompt="\n")
            try:
                result = api_probe.probe_endpoint(
                    base_url=base_url, key=key, claimed_model=claimed,
                )
            except Exception as exc:
                yield f"[api_probe] probe failed: {exc}\n"
                return StepOutcome({"error": str(exc)}, next_prompt="\n")
            yield (
                f"[api_probe] {result.entry_id} ok={result.ok} "
                f"score={result.weighted_average:.2f} guess={result.actual_model_guess} "
                f"matches={result.matches_claim}\n"
            )
            return StepOutcome(result.__dict__, next_prompt="\n")
        if action == 'probe_candidates':
            limit = int(args.get('limit') or 20)
            probed = []
            for cand in free_pool_vault.iter_candidates():
                if len(probed) >= limit:
                    break
                base_url = (cand.get('base_url') or '').strip()
                key = (cand.get('key') or '').strip()
                if not base_url or not key:
                    continue
                try:
                    result = api_probe.probe_endpoint(
                        base_url=base_url,
                        key=key,
                        claimed_model=cand.get('claimed_model') or '',
                        source=cand.get('source'),
                    )
                except Exception as exc:
                    yield f"[api_probe] {base_url} error: {exc}\n"
                    continue
                probed.append({
                    "entry_id": result.entry_id,
                    "ok": result.ok,
                    "score": result.weighted_average,
                    "guess": result.actual_model_guess,
                })
            yield f"[api_probe] probed {len(probed)} candidates\n"
            return StepOutcome({"probed": probed}, next_prompt="\n")
        return StepOutcome({"error": f"unknown action {action!r}"}, next_prompt="\n")

    def do_free_pool_ask(self, args, response):
        '''Single-turn LLM inference through the free pool, sensitivity-gated.'''
        from launcher import free_pool_router

        prompt = (args.get('prompt') or '').strip()
        if not prompt:
            yield "[free_pool_ask] prompt is required\n"
            return StepOutcome({"error": "missing_prompt"}, next_prompt="\n")
        sensitivity = (args.get('sensitivity') or 'public').strip().lower()
        fallback_hint = _bool_arg(args.get('fallback_to_paid'), False)
        try:
            result = free_pool_router.ask(
                prompt,
                sensitivity=sensitivity,
                max_attempts=int(args.get('max_attempts') or 3),
                max_tokens=int(args.get('max_tokens') or 1024),
                category_required=args.get('category_required') or None,
                min_score=float(args.get('min_score') or 0.0),
            )
        except free_pool_router.SensitivityError as exc:
            yield f"[free_pool_ask] sensitivity blocked: {exc}\n"
            return StepOutcome({"error": "sensitivity_blocked", "detail": str(exc)}, next_prompt="\n")
        if result["used"] == "free-pool":
            yield (
                f"[free_pool_ask] answered by {result['entry_id']} "
                f"after {len(result['tried'])} attempt(s)\n"
            )
        elif result["error"] == "free_pool_exhausted_no_fallback" and fallback_hint:
            # Translate the error so the agent SOP picks up the retry hint.
            result = {**result, "error": "free_pool_exhausted_retry_with_main_llm"}
            yield "[free_pool_ask] free pool exhausted; retry with main LLM\n"
        else:
            yield f"[free_pool_ask] no answer ({result.get('error', '')})\n"
        return StepOutcome(result, next_prompt="\n")

    def do_pm_friction_scan(self, args, response):
        '''PM Track A: deterministic friction-signal scan over recent activity logs.'''
        from tools import pm_friction_miner

        days = int(args.get('days_window') or pm_friction_miner.DEFAULT_WINDOW_DAYS)
        min_runs = int(args.get('min_distinct_runs') or pm_friction_miner.MIN_DISTINCT_RUNS)
        force = _bool_arg(args.get('force'), False)

        cadence = pm_friction_miner.check_cadence(track="A")
        if not force and not cadence["allowed"]:
            yield f"[pm_friction_scan] blocked by cadence: {cadence['reason']}\n"
            return StepOutcome(
                {"throttled": True, "cadence": cadence, "clusters": []},
                next_prompt="\n",
            )

        result = pm_friction_miner.scan(
            days_window=days,
            min_distinct_runs=min_runs,
        )
        pm_friction_miner.mark_run(track="A")

        next_id_hint = pm_friction_miner.next_proposal_id("A")
        proposals_md = pm_friction_miner.proposals_path()
        yield (
            f"[pm_friction_scan] {result['signal_total']} signals "
            f"→ {result['cluster_count_qualified']} qualified clusters "
            f"(mode={cadence['mode']}, accept_rate={cadence['accept_rate']})\n"
        )
        return StepOutcome(
            {
                **result,
                "cadence": cadence,
                "next_proposal_id_hint": next_id_hint,
                "proposals_path": str(proposals_md),
                "guidance": (
                    "现在请你自己读 clusters 做更智能的归因（≤5 个根因）。"
                    "对每个幸存根因，按 pm_role_sop.md 的模板把提案 append 到 proposals_path。"
                    "提案 id 从 next_proposal_id_hint 起递增。"
                    "写完后用 pm_proposal_decide 不是这里的事——那是用户事后审完才打的决策。"
                ) if cadence["mode"] == "normal" else (
                    "当前是反思模式（accept_rate < 30%）。**不要**新增提案；"
                    "请读 temp/pm_proposal_log.jsonl 中最近被 reject 的提案，"
                    "总结 ≤3 个『歪掉的方向』写到 temp/pm_reflection_YYYYMMDD.md。"
                ),
            },
            next_prompt="\n",
        )

    def do_pm_proposal_decide(self, args, response):
        '''Record a decision on a PM proposal for accept-rate tracking.'''
        from tools import pm_friction_miner

        proposal_id = (args.get('proposal_id') or '').strip()
        status = (args.get('status') or '').strip().lower()
        decided_by = (args.get('decided_by') or 'user').strip()
        track = (args.get('track') or 'A').strip()
        if not proposal_id or not status:
            yield "[pm_proposal_decide] proposal_id and status are required\n"
            return StepOutcome({"error": "missing_field"}, next_prompt="\n")
        try:
            pm_friction_miner.record_decision(
                proposal_id, status=status, decided_by=decided_by, track=track,
            )
        except ValueError as exc:
            yield f"[pm_proposal_decide] invalid status: {exc}\n"
            return StepOutcome({"error": "invalid_status"}, next_prompt="\n")
        stats = pm_friction_miner.compute_accept_rate(track=track)
        yield (
            f"[pm_proposal_decide] {proposal_id} → {status}; "
            f"accept_rate={stats['rate']} ({stats['accepted']}/"
            f"{stats['accepted'] + stats['rejected']})\n"
        )
        return StepOutcome({"recorded": True, "stats": stats}, next_prompt="\n")

    def do_mixture_of_agents(self, args, response):
        '''Fan-out + aggregate over multiple model configs.'''
        from tools.mixture_of_agents import run_moa
        prompt = (args.get('prompt') or '').strip()
        members = args.get('member_names') or []
        if not prompt or not isinstance(members, list) or not members:
            yield "[mixture_of_agents] prompt and member_names are required\n"
            return StepOutcome({"error": "missing_field"}, next_prompt="\n")
        try:
            timeout_s = float(args.get('timeout_s') or 180.0)
        except (TypeError, ValueError):
            timeout_s = 180.0
        result = run_moa(
            prompt=prompt,
            member_names=[str(m) for m in members],
            aggregator_name=(args.get('aggregator_name') or None),
            timeout_s=timeout_s,
        )
        yield f"[mixture_of_agents] {len(members)} members, {result['elapsed_ms']:.0f}ms total\n"
        return StepOutcome(result, next_prompt="\n")

    def do_voice(self, args, response):
        '''Audio I/O: transcribe / tts / tts_edge.'''
        from tools import voice_tools
        action = (args.get('action') or '').strip()
        if action == 'transcribe':
            audio_path = (args.get('audio_path') or '').strip()
            if not audio_path:
                yield "[voice] transcribe requires audio_path\n"
                return StepOutcome({"error": "missing_field"}, next_prompt="\n")
            text = voice_tools.transcribe(audio_path,
                                          model=str(args.get('model') or 'whisper-1'),
                                          language=args.get('language'))
            yield f"[voice] transcribe {audio_path!r}\n"
            return StepOutcome({"text": text}, next_prompt="\n")
        if action in ('tts', 'tts_edge'):
            text = (args.get('text') or '').strip()
            output_path = (args.get('output_path') or '').strip()
            if not text or not output_path:
                yield f"[voice] {action} requires text + output_path\n"
                return StepOutcome({"error": "missing_field"}, next_prompt="\n")
            if action == 'tts':
                path = voice_tools.tts(text, output_path,
                                       voice=str(args.get('voice') or 'alloy'),
                                       model=str(args.get('model') or 'tts-1'))
            else:
                path = voice_tools.tts_edge(text, output_path,
                                            voice=str(args.get('voice') or 'zh-CN-XiaoxiaoNeural'))
            yield f"[voice] {action} → {path}\n"
            return StepOutcome({"path": path}, next_prompt="\n")
        return StepOutcome({"error": f"unknown action {action!r}"}, next_prompt="\n")

    def do_image_generate(self, args, response):
        '''Generate images via /images/generations.'''
        from tools.image_generation import generate_image
        prompt = (args.get('prompt') or '').strip()
        output_path = (args.get('output_path') or '').strip()
        if not prompt or not output_path:
            yield "[image_generate] prompt + output_path required\n"
            return StepOutcome({"error": "missing_field"}, next_prompt="\n")
        try:
            n = int(args.get('n') or 1)
        except (TypeError, ValueError):
            n = 1
        path = generate_image(
            prompt=prompt,
            output_path=output_path,
            model=args.get('model'),
            size=str(args.get('size') or '1024x1024'),
            quality=str(args.get('quality') or 'standard'),
            n=max(1, n),
        )
        yield f"[image_generate] → {path}\n"
        return StepOutcome({"path": path}, next_prompt="\n")

    def do_skill_propose_patch(self, args, response):
        '''Queue a skill/SOP improvement proposal for user review.'''
        from memory import skill_self_improve as ssi
        skill_id = (args.get('skill_id') or '').strip()
        diff = args.get('diff') or ''
        reason = str(args.get('reason') or '')
        if not skill_id or not diff:
            yield "[skill_propose_patch] skill_id + diff required\n"
            return StepOutcome({"error": "missing_field"}, next_prompt="\n")
        try:
            prop = ssi.propose_patch(skill_id, diff, reason=reason)
        except Exception as exc:
            yield f"[skill_propose_patch] error: {exc}\n"
            return StepOutcome({"error": str(exc)}, next_prompt="\n")
        yield f"[skill_propose_patch] queued proposal {prop['id']} for {skill_id}\n"
        return StepOutcome({"proposal": prop}, next_prompt="\n")

    def do_no_tool(self, args, response):
        '''这是一个特殊工具，由引擎自主调用，不要包含在TOOLS_SCHEMA里。
        当模型在一轮中未显式调用任何工具时，由引擎自动触发。
        二次确认仅在回复几乎只包含<thinking>/<summary>和一段大代码块时触发。'''
        content = getattr(response, 'content', '') or ""
        thinking = getattr(response, 'thinking', '') or ""
        if not response or (not content.strip() and not thinking.strip()):
            yield "[Warn] LLM returned an empty response. Retrying...\n"
            return StepOutcome({}, next_prompt="[System] Blank response, regenerate and tooluse")
        if len(content) > 50 and ('未收到完整响应 !!!]' in content[-100:] or '!!!Error: [SSL:' in content[-100:]):
            return StepOutcome({}, next_prompt="[System] Incomplete response. Regenerate and tooluse.")
        if 'max_tokens !!!]' in content[-100:]:
            return StepOutcome({}, next_prompt="[System] max_tokens limit reached. Use multi small steps to do it.")
        
        if self._in_plan_mode() and any(kw in content for kw in ['任务完成', '全部完成', '已完成所有', '🏁']):
            if 'VERDICT' not in content and '[VERIFY]' not in content and '验证subagent' not in content:
                yield "[Warn] Plan模式完成声明拦截。\n"
                return StepOutcome({}, next_prompt="⛔ [验证拦截] 检测到你在plan模式下声称完成，但未执行[VERIFY]验证步骤。请先按plan_sop §四启动验证subagent，获得VERDICT后才能声称完成。")
            
        # 2. 检测"包含较大代码块但未调用工具"的情况
        # 关键特征：恰好1个大代码块 + 代码块直接结尾（后面只有空白）
        code_block_pattern = r"```[a-zA-Z0-9_]*\n[\s\S]{50,}?```"
        blocks = re.findall(code_block_pattern, content)
        if len(blocks) == 1:
            m = re.search(code_block_pattern, content)
            after_block = content[m.end():]
            if not after_block.strip():
                residual = content.replace(m.group(0), "")
                residual = re.sub(r"<thinking>[\s\S]*?</thinking>", "", residual, flags=re.IGNORECASE)
                residual = re.sub(r"<summary>[\s\S]*?</summary>", "", residual, flags=re.IGNORECASE)
                clean_residual = re.sub(r"\s+", "", residual)
                if len(clean_residual) <= 30:
                    yield "[Info] Detected large code block without tool call and no extra natural language. Requesting clarification.\n"
                    next_prompt = (
                        "[System] 检测到你在上一轮回复中主要内容是较大代码块，且本轮未调用任何工具。\n"
                        "如果这些代码需要执行、写入文件或进一步分析，请重新组织回复并显式调用相应工具"
                        "（例如：code_run、file_write、file_patch 等）；\n"
                        "如果只是向用户展示或讲解代码片段，请在回复中补充自然语言说明，"
                        "并明确是否还需要额外的实际操作。"
                    )
                    return StepOutcome({}, next_prompt=next_prompt)
                
        if self._in_plan_mode():
            remaining = self._check_plan_completion()
            if remaining == 0:
                self._exit_plan_mode(); yield "[Info] Plan完成：plan.md中0个[ ]残留，退出plan模式。\n"
        
        yield "[Info] Final response to user.\n"
        return StepOutcome(response, next_prompt=None)
    
    def do_start_long_term_update(self, args, response):
        '''Agent觉得当前任务完成后有重要信息需要记忆时调用此工具。'''
        prompt = '''### [总结提炼经验] 既然你觉得当前任务有重要信息需要记忆，请提取最近一次任务中【事实验证成功且长期有效】的环境事实、用户偏好、重要步骤，更新记忆。
本工具是标记开启结算过程，若已在更新记忆过程或没有值得记忆的点，忽略本次调用。
**如果没有经验证的，未来能用上的信息，忽略本次调用！**
**只能提取行动验证成功的信息**：
- **环境事实**（路径/凭证/配置）→ `file_patch` 更新 L2，同步 L1
- **复杂任务经验**（关键坑点/前置条件/重要步骤）→ L3 精简 SOP（只记你被坑得多次重试的核心要点）
**禁止**：临时变量、具体推理过程、未验证信息、通用常识、你可以轻松复现的细节、只是做了但没有验证的信息
**操作**：严格遵循提供的L0的记忆更新SOP。先 `file_read` 看现有 → 判断类型 → 最小化更新 → 无新内容跳过，保证对记忆库最小局部修改。\n
''' + get_global_memory()
        yield "[Info] Start distilling good memory for long-term storage.\n"
        path = './memory/memory_management_sop.md'
        if os.path.exists(path): result = '自动读取L0内容：\n' + file_read(path, show_linenos=False)
        else: result = "Memory Management SOP not found. Do not update memory."
        return StepOutcome(result, next_prompt=prompt)

    def _get_anchor_prompt(self, skip=False):
        if skip: return "\n"
        h_str = "\n".join(self.history_info[-40:])
        prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get('key_info'): prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        if self.working.get('related_sop'): prompt += f"\n有不清晰的地方请再次读取{self.working.get('related_sop')}"
        if getattr(self.parent, 'verbose', False):
            try: print(prompt)
            except (UnicodeEncodeError, OSError): pass  # 控制台编码/管道关闭等可忽略
        return prompt

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        _c = re.sub(r'```.*?```|<thinking>.*?</thinking>', '', response.content, flags=re.DOTALL)
        rsumm = re.search(r"<summary>(.*?)</summary>", _c, re.DOTALL)
        if rsumm: summary = rsumm.group(1).strip()
        else:
            tc = tool_calls[0]; tool_name, args = tc['tool_name'], tc['args']   # at least one because no_tool
            clean_args = {k: v for k, v in args.items() if not k.startswith('_')}
            summary = f"调用工具{tool_name}, args: {clean_args}"
            if tool_name == 'no_tool': summary = "直接回答了用户问题"
            next_prompt += "\n[DANGER] 你遗漏了<summary>，必须按协议一直在每次回复中用<summary>中输出极简单行摘要！" 
        summary = smart_format(summary, max_str_len=100)
        self.history_info.append(f'[Agent] {summary}')
        activity_log.record({
            'phase': 'turn_end',
            'turn': turn,
            'summary': summary,
            'exit_reason': exit_reason or {},
            'related_sop': self.working.get('related_sop') or '',
        })
        if turn % 65 == 0 and 'plan' not in str(self.working.get('related_sop')):
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。你必须总结情况进行ask_user，不允许继续重试。"
        elif turn % 7 == 0:
            next_prompt += f"\n\n[DANGER] 已连续执行第 {turn} 轮。禁止无效重试。若无有效进展，必须切换策略：1. 探测物理边界 2. 请求用户协助。如有需要，可调用 update_working_checkpoint 保存关键上下文。"
        elif turn % 10 == 0: next_prompt += get_global_memory()

        if (_plan := self._in_plan_mode()) and turn >= 10 and turn % 5 == 0:
            next_prompt = f"[Plan Hint] 你正在计划模式。必须 file_read({_plan}) 确认当前步骤，回复开头引用：📌 当前步骤：...\n\n" + next_prompt
        if _plan and turn >= 90: next_prompt += f"\n\n[DANGER] Plan模式已运行 {turn} 轮，已达上限。必须 ask_user 汇报进度并确认是否继续。"

        injkeyinfo = consume_file(self.parent.task_dir, '_keyinfo')
        injprompt = consume_file(self.parent.task_dir, '_intervene')
        if injkeyinfo: self.working['key_info'] = self.working.get('key_info', '') + f"\n[MASTER] {injkeyinfo}"
        if injprompt: next_prompt += f"\n\n[MASTER] {injprompt}\n"
        for hook in getattr(self.parent, '_turn_end_hooks', {}).values(): hook(locals())  # current readonly
        return next_prompt

def get_global_memory():
    prompt = "\n"
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        suffix = '_en' if os.environ.get('WLWL_LANG', '') == 'en' else ''
        with open(os.path.join(script_dir, 'memory/global_mem_insight.txt'), 'r', encoding='utf-8', errors='replace') as f: insight = f.read()
        with open(os.path.join(script_dir, f'assets/insight_fixed_structure{suffix}.txt'), 'r', encoding='utf-8') as f: structure = f.read()
        prompt += f'cwd = {os.path.join(script_dir, "temp")} (./)\n'
        prompt += f"\n[Memory] (../memory)\n"
        prompt += structure + '\n../memory/global_mem_insight.txt:\n'
        prompt += insight + "\n"
    except FileNotFoundError: pass
    return prompt
