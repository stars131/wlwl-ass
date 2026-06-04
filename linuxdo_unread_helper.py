"""
Linux.do 未读阅读助手 - 一键启动器
通过 CDP bridge (TMWebDriver) 找到已打开的 Linux.do 标签页，
注入手动未读阅读助手浮窗。
"""
import sys, json, time, requests

BRIDGE_HOST = "127.0.0.1"
BRIDGE_HTTP_PORT = 18766  # TMWebDriver HTTP API port = ws_port + 1
URL_PATTERN = "linux.do"
TIMEOUT = 10

JS_CODE = r"""
(() => {
  const PANEL_ID = 'ga-manual-unread-helper';
  if (document.getElementById(PANEL_ID)) {
    const el = document.getElementById(PANEL_ID);
    el.style.display = el.style.display === 'none' ? 'block' : el.style.display;
    return 'helper_already_exists_toggled';
  }

  // --- Local read-tracking via localStorage ---
  const READ_KEY = 'ga_helper_read_ids';
  function getReadIds() { try { return new Set(JSON.parse(localStorage.getItem(READ_KEY)||'[]')); } catch(e) { return new Set(); } }
  function saveReadIds(ids) { try { localStorage.setItem(READ_KEY, JSON.stringify([...ids])); } catch(e) {} }
  function markRead(id) { const ids = getReadIds(); ids.add(id); saveReadIds(ids); }

  // --- Fetch latest topics, filter by local read-tracking ---
  async function fetchUnread() {
    const items = [];
    const readIds = getReadIds();
    // 1) Latest topics (always fresh)
    try {
      const r = await fetch('/latest.json?order=activity', {credentials:'include'});
      const d = await r.json();
      (d.topic_list?.topics || []).forEach(t => {
        if (!readIds.has(t.id) && !items.find(x=>x.id===t.id))
          items.push({id:t.id, title:t.title, slug:t.slug, status:'最新'});
      });
    } catch(e) {}
    // 2) Also include server-tracked unread (these may already be read locally)
    try {
      const r2 = await fetch('/unread.json?filter=default', {credentials:'include'});
      const d2 = await r2.json();
      (d2.topic_list?.topics || []).forEach(t => {
        if (!readIds.has(t.id) && !items.find(x=>x.id===t.id))
          items.push({id:t.id, title:t.title, slug:t.slug, status:'未读'});
      });
    } catch(e) {}
    // 3) Server-tracked new topics
    try {
      const r3 = await fetch('/new.json?filter=default', {credentials:'include'});
      const d3 = await r3.json();
      (d3.topic_list?.topics || []).forEach(t => {
        if (!readIds.has(t.id) && !items.find(x=>x.id===t.id))
          items.push({id:t.id, title:t.title, slug:t.slug, status:'新帖'});
      });
    } catch(e) {}
    return items;
  }

  function buildPanel(items) {
    const panel = document.createElement('div');
    panel.id = PANEL_ID;
    Object.assign(panel.style, {
      position:'fixed', bottom:'20px', right:'20px', width:'380px', maxHeight:'70vh',
      background:'#fff', border:'2px solid #e4590e', borderRadius:'10px',
      boxShadow:'0 4px 20px rgba(0,0,0,.18)', zIndex:'999999', fontFamily:'sans-serif',
      overflow:'hidden', resize:'both'
    });

    let isDragging = false, dragOff = {x:0,y:0};
    panel.addEventListener('mousedown', e => {
      if (e.target.tagName === 'BUTTON' || e.target.tagName === 'A') return;
      isDragging = true; dragOff.x = e.clientX - panel.offsetLeft; dragOff.y = e.clientY - panel.offsetTop;
    });
    document.addEventListener('mousemove', e => { if(isDragging){ panel.style.left=(e.clientX-dragOff.x)+'px'; panel.style.top=(e.clientY-dragOff.y)+'px'; panel.style.right='auto'; panel.style.bottom='auto'; }});
    document.addEventListener('mouseup', () => isDragging = false);

    const header = document.createElement('div');
    Object.assign(header.style, {background:'#e4590e', color:'#fff', padding:'8px 12px', cursor:'move', display:'flex', justifyContent:'space-between', alignItems:'center', userSelect:'none'});
    header.innerHTML = '<span style="font-weight:bold">📖 未读阅读助手（手动）</span><span></span>';

    const closeBtn = document.createElement('span');
    closeBtn.textContent = '✕'; closeBtn.style.cursor = 'pointer'; closeBtn.style.fontSize = '18px';
    closeBtn.onclick = () => panel.remove();
    const collapseBtn = document.createElement('span');
    collapseBtn.textContent = '▬'; collapseBtn.style.cursor = 'pointer'; collapseBtn.style.marginRight = '10px'; collapseBtn.style.fontSize = '14px';
    header.querySelector('span:last-child').append(collapseBtn, closeBtn);

    const body = document.createElement('div');
    body.id = PANEL_ID + '-body';
    Object.assign(body.style, {padding:'10px', overflowY:'auto', maxHeight:'calc(70vh - 100px)'});

    const state = { queue: items, idx: 0, opened: new Set(), skipped: new Set(), currentUrl: null };

    function renderList() {
      body.innerHTML = '';
      const info = document.createElement('div');
      info.style.cssText = 'font-size:12px;color:#666;margin-bottom:8px;';
      info.textContent = `队列: ${state.queue.length - state.idx} 未读 | 已开: ${state.opened.size} | 已跳: ${state.skipped.size}`;
      body.appendChild(info);

      state.queue.slice(state.idx, state.idx + 15).forEach((item, i) => {
        const row = document.createElement('div');
        row.style.cssText = 'padding:4px 6px;border-bottom:1px solid #eee;font-size:13px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        row.textContent = `${state.idx+i+1}. ${item.title}`;
        row.title = item.title;
        row.onmouseenter = () => row.style.background = '#fff3e0';
        row.onmouseleave = () => row.style.background = '';
        row.onclick = () => openTopic(state.idx + i);
        body.appendChild(row);
      });
    }

    function openTopic(idx) {
      if (idx >= state.queue.length) return;
      const item = state.queue[idx];
      state.idx = idx;
      state.opened.add(item.id);
      markRead(item.id);  // Track as read locally
      const url = '/t/' + (item.slug || 'topic') + '/' + item.id;
      state.currentUrl = url;
      // Use Ember router for SPA navigation (keeps panel alive)
      try {
        const container = document.getElementById('main-outlet') || document.querySelector('.ember-application');
        if (window.DiscourseURL && window.DiscourseURL.routeTo) {
          window.DiscourseURL.routeTo(url);
        } else if (window.Discourse && window.Discourse.__container__) {
          const router = window.Discourse.__container__.lookup('router:main');
          router.transitionTo(url);
        } else {
          // fallback: load into a container via fetch
          loadInPage(url);
        }
      } catch(e) { loadInPage(url); }
      renderList();
    }

    async function loadInPage(url) {
      try {
        const resp = await fetch(url, {credentials:'include'});
        const html = await resp.text();
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const content = doc.querySelector('#main-outlet') || doc.querySelector('.topic-post') || doc.querySelector('.post-stream');
        const outlet = document.querySelector('#main-outlet') || document.querySelector('.ember-application');
        if (content && outlet) {
          outlet.innerHTML = content.innerHTML;
        }
      } catch(e) { window.location.href = url; }
    }

    function nextTopic() {
      if (state.idx + 1 < state.queue.length) { state.idx++; openTopic(state.idx); }
      else { body.innerHTML = '<div style="color:#e4590e;font-weight:bold;text-align:center;padding:20px">✅ 队列已读完！点「刷新队列」重新抓取</div>'; }
    }

    function skipTopic() {
      if (state.idx < state.queue.length) { state.skipped.add(state.queue[state.idx].id); state.idx++; openTopic(state.idx); }
    }

    function refreshQueue() {
      body.innerHTML = '<div style="text-align:center;padding:20px;color:#e4590e">⏳ 刷新中...</div>';
      fetchUnread().then(items => {
        state.queue = items; state.idx = 0; state.opened.clear(); state.skipped.clear();
        renderList();
      });
    }

    function clearRecord() { state.opened.clear(); state.skipped.clear(); state.idx = 0; renderList(); }

    // --- Keyboard shortcuts ---
    function onKeydown(e) {
      // Only trigger when not typing in an input/textarea
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
      // Alt+N → next topic
      if (e.altKey && e.key.toLowerCase() === 'n') { e.preventDefault(); nextTopic(); }
      // Alt+R → refresh queue
      if (e.altKey && e.key.toLowerCase() === 'r') { e.preventDefault(); refreshQueue(); }
      // Alt+S → skip current
      if (e.altKey && e.key.toLowerCase() === 's') { e.preventDefault(); skipTopic(); }
    }
    document.addEventListener('keydown', onKeydown);
    // Clean up listener when panel is removed
    const origRemove = panel.remove.bind(panel);
    panel.remove = () => { document.removeEventListener('keydown', onKeydown); origRemove(); };

    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid #eee;';

    function mkBtn(text, fn, color='#e4590e', shortcut='') {
      const b = document.createElement('button');
      b.innerHTML = text + (shortcut ? ` <span style="font-size:11px;opacity:.7">[${shortcut}]</span>` : '');
      b.onclick = fn;
      b.style.cssText = `padding:6px 10px;border:none;border-radius:5px;background:${color};color:#fff;cursor:pointer;font-size:13px;`;
      return b;
    }
    btnRow.append(mkBtn('下一个未读 ➡️', nextTopic, '#e4590e', 'Alt+N'), mkBtn('跳过当前 ⏭️', skipTopic, '#999', 'Alt+S'), mkBtn('刷新队列 🔄', refreshQueue, '#2196f3', 'Alt+R'), mkBtn('清空记录', clearRecord, '#888'));

    let collapsed = false;
    collapseBtn.onclick = () => {
      collapsed = !collapsed;
      body.style.display = collapsed ? 'none' : 'block';
      btnRow.style.display = collapsed ? 'none' : 'flex';
    };

    panel.append(header, body, btnRow);
    document.body.appendChild(panel);
    renderList();
    return 'manual_unread_helper_started';
  }

  fetchUnread().then(items => buildPanel(items)).catch(e => {
    buildPanel([]);
  });
})();
"""

def find_linuxdo_session():
    """Find a browser tab with linux.do via CDP bridge."""
    url = f"http://{BRIDGE_HOST}:{BRIDGE_HTTP_PORT}/link"
    try:
        resp = requests.post(url, json={"cmd": "find_session", "url_pattern": URL_PATTERN}, timeout=TIMEOUT)
        data = resp.json()
        sessions = data.get("r", [])
        if sessions:
            return sessions[0] if isinstance(sessions, list) else sessions
    except Exception as e:
        print(f"[ERROR] Cannot reach CDP bridge: {e}")
    return None

def execute_js_in_session(session_id, code):
    """Execute JS in a specific browser session via CDP bridge."""
    url = f"http://{BRIDGE_HOST}:{BRIDGE_HTTP_PORT}/link"
    try:
        resp = requests.post(url, json={
            "cmd": "execute_js",
            "sessionId": session_id,
            "code": code,
            "timeout": 15.0
        }, timeout=20)
        data = resp.json()
        return data.get("r")
    except Exception as e:
        print(f"[ERROR] JS execution failed: {e}")
        return None

def main():
    print("=" * 50)
    print("  📖 Linux.do 未读阅读助手 启动器")
    print("=" * 50)

    print("\n[1/2] 正在查找 Linux.do 标签页...")
    session = find_linuxdo_session()

    if not session:
        print("❌ 未找到已打开的 Linux.do 标签页！")
        print("   请先在 Chrome 中打开 https://linux.do 并确保 CDP bridge 扩展已连接。")
        input("\n按回车退出...")
        sys.exit(1)

    session_id = session.get("id") or session.get("sessionId")
    session_url = session.get("url", "")
    print(f"✅ 找到标签页: {session_url}")
    print(f"   Session ID: {session_id}")

    print("\n[2/2] 正在注入未读阅读助手...")
    result = execute_js_in_session(session_id, JS_CODE)

    if result and not (isinstance(result, dict) and result.get("error")):
        print(f"✅ 注入成功！结果: {result}")
        print("\n   🎉 现在可以在 Linux.do 页面右下角看到浮窗了！")
        print("   使用方法：")
        print("   - 点「下一个未读」：手动打开下一篇")
        print("   - 点「跳过当前」：跳过这篇")
        print("   - 点「刷新队列」：重新抓取未读帖子")
        print("   - 点 ✕：关闭浮窗")
    else:
        print(f"❌ 注入失败: {result}")
        print("   请确认 CDP bridge 扩展正常连接，然后重试。")

    input("\n按回车退出...")

if __name__ == "__main__":
    main()