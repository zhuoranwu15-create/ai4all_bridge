function getToken() {
  var token = localStorage.getItem('admin_token');
  if (!token) {
    token = prompt('请输入 Admin / Staff Token：');
    if (token) localStorage.setItem('admin_token', token.trim());
  }
  return token;
}

async function loadAdminMe() {
  try {
    var data = await apiFetch('/admin/me');
    window.currentAdminUser = data.admin_user;
    var el = document.getElementById('admin-user');
    if (el) {
      el.textContent = data.admin_user.display_name + ' · ' + data.admin_user.role;
    }
    return data.admin_user;
  } catch (e) {
    return null;
  }
}

function resolveOpsApiPath(path) {
  var inOps = window.location.pathname === '/ops' || window.location.pathname.indexOf('/ops/') === 0;
  if (!inOps) return path;
  if (path === '/admin' || path.indexOf('/admin/') === 0) return '/ops' + path;
  if (path === '/debug' || path.indexOf('/debug/') === 0) return '/ops' + path;
  if (path === '/openclaw' || path.indexOf('/openclaw/') === 0) return '/ops' + path;
  return path;
}

async function apiFetch(path, options) {
  options = options || {};
  var token = getToken();
  var headers = { 'Authorization': 'Bearer ' + token };
  if (options.body) headers['Content-Type'] = 'application/json';

  var res = await fetch(resolveOpsApiPath(path), Object.assign({}, options, {
    headers: Object.assign(headers, options.headers || {}),
  }));

  if (res.status === 401) {
    localStorage.removeItem('admin_token');
    throw new Error('认证失败，请刷新页面重新输入 Token');
  }
  if (!res.ok) {
    var body = await res.text();
    throw new Error('API 错误 ' + res.status + ': ' + body);
  }
  return res.json();
}

function esc(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showStatus(el, msg, isError) {
  el.textContent = msg;
  el.className = 'status-msg ' + (isError ? 'error' : 'success');
  if (!isError) setTimeout(function() { el.textContent = ''; }, 3000);
}

function contentLabel(item) {
  var chars = item && item.content_redacted ? item.content_chars : 0;
  return '已脱敏' + (chars ? ' · ' + chars + ' 字' : '');
}

function formatDateTime(value) {
  if (!value) return '—';
  var s = String(value).replace(' ', 'T');
  if (!s.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
  var d = new Date(s);
  if (isNaN(d.getTime())) return String(value).slice(0, 16);
  var y = d.getFullYear();
  var mo = String(d.getMonth() + 1).padStart(2, '0');
  var dy = String(d.getDate()).padStart(2, '0');
  var h = String(d.getHours()).padStart(2, '0');
  var mi = String(d.getMinutes()).padStart(2, '0');
  return y + '-' + mo + '-' + dy + ' ' + h + ':' + mi;
}
