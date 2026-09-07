/* 推理精度异常监控 Web 界面前端逻辑
 *
 * - 无构建：原生 JS + ECharts CDN。
 * - 登录：POST /api/login 存 token，后续请求带 Authorization: Bearer <token>；
 *   401 → 跳登录页。
 * - 短轮询 3s：看板轮询 /api/summary /api/instances /api/alerts /api/trends；
 *   详情页轮询 /api/instances/{name}/summary /api/instances/{name}/trends；
 *   静默失败不打扰（实例离线仅状态灯变化）；实例管理操作成功后主动刷新一次。
 * - 告警：未读数持久化，用户点开告警面板前一直显示在铃铛上；点击铃铛/横幅展开
 *   告警面板查看详情；横幅按 id 去重、3s 自动消失，不影响未读数。
 */
(function () {
  'use strict';

  var POLL_MS = 3000;
  var BANNER_MS = 3000;
  var ALERT_SEEN_KEY = 'webui_alert_seen_id';

  var ILL_TYPES = ['rare_character', 'garbled', 'repetition', 'nan_value'];
  var ILL_LABELS = {
    rare_character: '生僻字',
    garbled: '乱码',
    repetition: '重复',
    nan_value: 'NaN 值',
    unknown: '未知',
  };
  var ILL_COLORS = {
    rare_character: '#5470c6',
    garbled: '#91cc75',
    repetition: '#fac858',
    nan_value: '#ee6666',
    unknown: '#9ca3af',
  };
  var KPI_DEFS = [
    { key: 'anomalies', label: '总异常检出', accent: 'red' },
    { key: 'rare_character', label: '生僻字检测数量' },
    { key: 'garbled', label: '乱码检出数量' },
    { key: 'repetition', label: '重复检出数量' },
    { key: 'nan_value', label: 'NaN 检出数量' },
  ];

  var TOKEN_KEY = 'webui_token';
  var USER_KEY = 'webui_user';

  // ------------------------------------------------------------------ //
  // 工具
  // ------------------------------------------------------------------ //
  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function fmtTime(ts) {
    if (!ts) return '-';
    var d = new Date(ts * 1000);
    function p(x) {
      return (x < 10 ? '0' : '') + x;
    }
    return (
      p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
    );
  }

  function fmtNum(n) {
    if (n == null) return '0';
    return Number(n).toLocaleString('zh-CN');
  }

  function illLabel(name) {
    return ILL_LABELS[name] || ILL_LABELS.unknown;
  }

  function illTag(name) {
    return '<span class="tag tag-' + (name || 'unknown') + '">' + illLabel(name) + '</span>';
  }

  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function setAuth(token, user) {
    localStorage.setItem(TOKEN_KEY, token || '');
    if (user) localStorage.setItem(USER_KEY, user);
  }

  function clearAuth() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  function authHeaders(extra) {
    var h = Object.assign({}, extra || {});
    var t = getToken();
    if (t) h['Authorization'] = 'Bearer ' + t;
    return h;
  }

  async function api(path, options) {
    var opts = Object.assign({}, options || {});
    opts.headers = authHeaders(opts.headers);
    if (opts.body && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    var resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      return { ok: false, status: 0, data: null, network: true };
    }
    if (resp.status === 401) {
      showLogin();
      return { ok: false, status: 401, data: null };
    }
    var data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    return { ok: resp.ok, status: resp.status, data: data };
  }

  function showToast(msg, isError) {
    var t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' toast-error' : '');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      t.className = 'toast hidden';
    }, 2600);
  }

  function setView(view) {
    $('dashboard').classList.toggle('hidden', view !== 'dashboard');
    $('instance-detail').classList.toggle('hidden', view !== 'detail');
    closeAlertPanel();
  }

  function resizeCharts() {
    [trendChart, typeChart, modelChart, detailTrendChart, detailTypeChart].forEach(function (ch) {
      if (ch) ch.resize();
    });
  }

  // ------------------------------------------------------------------ //
  // 页面切换 / 登录
  // ------------------------------------------------------------------ //
  function showLogin() {
    clearAuth();
    closeAlertPanel();
    $('login-page').classList.remove('hidden');
    $('dashboard').classList.add('hidden');
    $('instance-detail').classList.add('hidden');
    stopPolling();
  }

  function showDashboard(user) {
    $('login-page').classList.add('hidden');
    if (user) {
      $('current-user').textContent = user;
      $('detail-current-user').textContent = user;
    }
    setView('dashboard');
    setTimeout(resizeCharts, 0);
    startPolling();
  }

  async function handleLogin(ev) {
    ev.preventDefault();
    var username = $('login-username').value.trim();
    var password = $('login-password').value;
    $('login-error').textContent = '';
    if (!username || !password) {
      $('login-error').textContent = '请输入用户名和密码';
      return;
    }
    var btn = $('login-btn');
    btn.disabled = true;
    var r = await api('/api/login', { method: 'POST', body: { username: username, password: password } });
    btn.disabled = false;
    if (r.ok && r.data && r.data.token) {
      setAuth(r.data.token, r.data.user || username);
      showDashboard(r.data.user || username);
      refreshNow();
    } else {
      $('login-error').textContent = '用户名或密码错误';
    }
  }

  function logout() {
    showLogin();
  }

  // ------------------------------------------------------------------ //
  // 图表初始化
  // ------------------------------------------------------------------ //
  var trendChart, typeChart, modelChart, detailTrendChart, detailTypeChart;

  function initCharts() {
    if (typeof echarts === 'undefined') {
      setTimeout(initCharts, 300);
      return;
    }
    trendChart = echarts.init($('trend-chart'));
    typeChart = echarts.init($('type-chart'));
    modelChart = echarts.init($('model-chart'));
    detailTrendChart = echarts.init($('detail-trend-chart'));
    detailTypeChart = echarts.init($('detail-type-chart'));
    window.addEventListener('resize', function () {
      trendChart.resize();
      typeChart.resize();
      modelChart.resize();
      detailTrendChart.resize();
      detailTypeChart.resize();
    });
  }

  function trendOption() {
    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        formatter: function (params) {
          var p = params[0];
          if (!p || !p.data) return '';
          var d = p.data;
          var time = fmtTime(d.value[0] / 1000);
          var tag = d.source === 'imported' ? ' <span class="tag tag-imported">导入</span>' : '';
          return (
            '累计：' + d.value[1] + '<br>' +
            '模型：' + (d.model || '-') + '<br>' +
            '检出类别：' + illLabel(d.ill_type) + tag + '<br>' +
            '检出时间：' + time
          );
        },
      },
      grid: { left: 56, right: 24, top: 24, bottom: 32 },
      xAxis: {
        type: 'time',
        axisLabel: { hideOverlap: true },
        axisLine: { lineStyle: { color: '#000' } },
        axisTick: { alignWithLabel: true },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        name: '累计异常数',
        nameLocation: 'middle',
        nameGap: 34,
        splitLine: { lineStyle: { color: '#eef1f6' } },
      },
      dataZoom: [{ type: 'inside' }],
      series: [
        {
          type: 'line',
          step: 'end',
          showSymbol: false,
          lineStyle: { width: 2, color: '#2563eb' },
          itemStyle: { color: '#2563eb' },
          data: [],
        },
      ],
    };
  }

  function modelBarLayout(chart, n) {
    // 固定柱宽 + 等间距：柱子过密时优先压缩间距，仍放不下再压缩柱宽
    var w = chart.getWidth() || (chart.getDom() && chart.getDom().clientWidth) || 680;
    var gridLeft = 40;
    var gridRight = 16;
    var plotW = Math.max(40, w - gridLeft - gridRight);
    var baseBar = 32;
    var desiredGap = 14;
    var minGap = 3;
    var minBar = 4;
    var barWidth, gap;
    if (n <= 1) {
      barWidth = Math.min(baseBar, plotW * 0.4);
      gap = desiredGap;
    } else {
      var barsW = n * baseBar;
      if (barsW + (n - 1) * desiredGap <= plotW) {
        barWidth = baseBar;
        gap = desiredGap;
      } else if (barsW + (n - 1) * minGap <= plotW) {
        barWidth = baseBar;
        gap = (plotW - barsW) / (n - 1);
      } else {
        gap = minGap;
        barWidth = Math.max(minBar, (plotW - (n - 1) * gap) / n);
      }
    }
    return {
      barWidth: barWidth,
      barCategoryGap: (gap / barWidth) * 100 + '%',
    };
  }

  // ------------------------------------------------------------------ //
  // KPI 渲染
  // ------------------------------------------------------------------ //
  function kpiValue(s, key) {
    return key === 'anomalies' ? s.anomalies : ((s.by_type || {})[key] || 0);
  }

  function renderKpis(prefix, s, wrapId) {
    if (wrapId) {
      // 详情页：卡片动态生成，无 id，按 .kpi-value 顺序写入
      var cards = $(wrapId).querySelectorAll('.kpi-card');
      cards.forEach(function (card, i) {
        card.querySelector('.kpi-value').textContent = fmtNum(kpiValue(s, KPI_DEFS[i].key));
      });
      return;
    }
    KPI_DEFS.forEach(function (d) {
      var node = $(prefix + d.key);
      if (node) node.textContent = fmtNum(kpiValue(s, d.key));
    });
  }

  // ------------------------------------------------------------------ //
  // 渲染：汇总 + 分布图
  // ------------------------------------------------------------------ //
  function renderSummary(s) {
    renderKpis('kpi-', s);

    // 异常类型分布饼图（累计）
    var pieData = ILL_TYPES.map(function (t) {
      return { name: illLabel(t), value: s.by_type[t] || 0 };
    }).filter(function (d) {
      return d.value > 0;
    });
    if (pieData.length === 0 || !typeChart) {
      $('type-chart').classList.add('hidden');
      $('type-empty').classList.remove('hidden');
    } else {
      $('type-chart').classList.remove('hidden');
      $('type-empty').classList.add('hidden');
      typeChart.setOption(
        {
          tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
          legend: { bottom: 0 },
          series: [
            {
              name: '异常类型',
              type: 'pie',
              radius: ['45%', '70%'],
              center: ['50%', '46%'],
              avoidLabelOverlap: true,
              label: { show: false },
              emphasis: { label: { show: true, fontWeight: 'bold' } },
              data: pieData,
            },
          ],
        }
      );
    }

    // 按实例累计异常柱状图（固定柱宽 + 等间距自适应）
    var instanceEntries = Object.entries(s.by_instance || {}).sort(function (a, b) {
      return b[1] - a[1];
    });
    if (instanceEntries.length === 0 || !modelChart) {
      $('model-chart').classList.add('hidden');
      $('model-empty').classList.remove('hidden');
    } else {
      $('model-chart').classList.remove('hidden');
      $('model-empty').classList.add('hidden');
      var layout = modelBarLayout(modelChart, instanceEntries.length);
      modelChart.setOption(
        {
          tooltip: { trigger: 'axis' },
          grid: { left: 40, right: 16, top: 24, bottom: 40 },
          xAxis: {
            type: 'category',
            data: instanceEntries.map(function (m) {
              return m[0];
            }),
            axisLabel: {
              interval: 0,
              rotate: instanceEntries.length > 8 ? 40 : instanceEntries.length > 4 ? 30 : 0,
            },
          },
          yAxis: { type: 'value', minInterval: 1 },
          series: [
            {
              type: 'bar',
              barWidth: layout.barWidth,
              barCategoryGap: layout.barCategoryGap,
              itemStyle: { color: '#2563eb', borderRadius: [3, 3, 0, 0] },
              data: instanceEntries.map(function (m) {
                return m[1];
              }),
            },
          ],
        }
      );
    }
  }

  function isEmptyTrend(points) {
    return !points || points.length === 0;
  }

  function renderTrend(chart, chartEl, emptyEl, points) {
    if (!chart) return;
    if (isEmptyTrend(points)) {
      chartEl.classList.add('hidden');
      emptyEl.classList.remove('hidden');
      return;
    }
    chartEl.classList.remove('hidden');
    emptyEl.classList.add('hidden');

    var data = (points || []).map(function (p) {
      return {
        value: [p.ts * 1000, p.cumulative],
        model: p.model,
        ill_type: p.ill_type,
        instance: p.instance,
        source: p.source,
      };
    });
    var opt = trendOption();
    opt.series[0].data = data;
    // 合并模式更新：animation:false 无闪烁，保留 dataZoom 缩放状态
    chart.setOption(opt);
  }

  // ------------------------------------------------------------------ //
  // 渲染：实例表格
  // ------------------------------------------------------------------ //
  function stateLabel(inst) {
    if (inst.paused) return '已暂停';
    if (inst.state === 'online') return '在线';
    if (inst.state === 'offline') return '离线';
    return inst.state || '未知';
  }

  function stateDotClass(inst) {
    if (inst.paused) return 'paused';
    if (inst.state === 'online') return 'online';
    return 'offline';
  }

  function renderInstances(instances) {
    var tb = $('instances-table').querySelector('tbody');
    tb.innerHTML = '';

    if (!instances || instances.length === 0) {
      var tr = el('tr', 'empty-row');
      var td = el('td', '', '暂无实例，请在下方向添加');
      td.colSpan = 8;
      tr.appendChild(td);
      tb.appendChild(tr);
      return;
    }

    instances.forEach(function (inst) {
      var tr = el('tr');

      tr.appendChild(el('td', 'inst-name-cell', inst.name));

      var models = (inst.models || []).join(', ');
      tr.appendChild(el('td', models ? '' : 'muted', models || '-'));

      tr.appendChild(el('td', '', fmtNum(inst.anomalies)));
      tr.appendChild(el('td', '', fmtNum(inst.errors)));

      var tdLast = el('td');
      if (inst.last_event) {
        tdLast.innerHTML =
          illTag(inst.last_event.ill_type) +
          ' <span class="muted">· ' + fmtTime(inst.last_event.ts) + '</span>';
      } else {
        tdLast.textContent = '—';
        tdLast.className = 'muted';
      }
      tr.appendChild(tdLast);

      var tdState = el('td');
      var status = el('span', 'inst-status');
      status.appendChild(el('span', 'inst-dot ' + stateDotClass(inst)));
      status.appendChild(el('span', '', stateLabel(inst)));
      tdState.appendChild(status);
      tr.appendChild(tdState);

      var tdActions = el('td');
      var actions = el('div', 'inst-actions');
      var paused = !!inst.paused;
      var pauseBtn = el('button', 'btn btn-xs btn-ghost', paused ? '恢复' : '暂停');
      pauseBtn.onclick = function () {
        togglePause(inst, paused);
      };
      var delBtn = el('button', 'btn btn-xs btn-danger', '删除');
      delBtn.onclick = function () {
        deleteInstance(inst);
      };
      actions.appendChild(pauseBtn);
      actions.appendChild(delBtn);
      tdActions.appendChild(actions);
      tr.appendChild(tdActions);

      var tdDetail = el('td');
      var detailBtn = el('button', 'btn btn-xs btn-primary', '详情');
      detailBtn.onclick = function () {
        openDetail(inst.name);
      };
      tdDetail.appendChild(detailBtn);
      tr.appendChild(tdDetail);

      tb.appendChild(tr);
    });
  }

  // ------------------------------------------------------------------ //
  // 详情页：KPI + 趋势 + 类型分布
  // ------------------------------------------------------------------ //
  var currentInstance = null;
  var detailWindow = '1day';

  function buildDetailKpis() {
    var wrap = $('detail-kpis');
    wrap.innerHTML = '';
    KPI_DEFS.forEach(function (d) {
      var card = el('div', 'kpi-card' + (d.accent ? ' accent-' + d.accent : ''));
      card.appendChild(el('div', 'kpi-label', d.label));
      card.appendChild(el('div', 'kpi-value', '0'));
      card.appendChild(el('div', 'kpi-sub', '累计'));
      wrap.appendChild(card);
    });
  }

  function openDetail(name) {
    currentInstance = name;
    buildDetailKpis();
    $('detail-instance-name').textContent = name;
    $('detail-instance-state').textContent = '';
    $('detail-current-user').textContent = $('current-user').textContent || '';
    detailWindow = '1day';
    var seg = $('detail-window-seg');
    seg.querySelectorAll('.seg-btn').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-window') === '1day');
    });
    setView('detail');
    $('detail-import-instance-name').textContent = name;
    setTimeout(function () {
      detailTrendChart.resize();
      detailTypeChart.resize();
    }, 0);
    refreshNow();
  }

  function closeDetail() {
    currentInstance = null;
    setView('dashboard');
    setTimeout(resizeCharts, 0);
    refreshNow();
  }

  function renderDetailSummary(s) {
    renderKpis(null, s, 'detail-kpis');
    $('detail-instance-state').textContent = stateLabel(s) + ' · 异常累计 ' + fmtNum(s.anomalies);

    var pieData = ILL_TYPES.map(function (t) {
      return { name: illLabel(t), value: (s.by_type || {})[t] || 0 };
    }).filter(function (d) {
      return d.value > 0;
    });
    if (pieData.length === 0 || !detailTypeChart) {
      $('detail-type-chart').classList.add('hidden');
      $('detail-type-empty').classList.remove('hidden');
    } else {
      $('detail-type-chart').classList.remove('hidden');
      $('detail-type-empty').classList.add('hidden');
      detailTypeChart.setOption(
        {
          tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
          legend: { bottom: 0 },
          series: [
            {
              name: '异常类型',
              type: 'pie',
              radius: ['45%', '70%'],
              center: ['50%', '46%'],
              avoidLabelOverlap: true,
              label: { show: false },
              emphasis: { label: { show: true, fontWeight: 'bold' } },
              data: pieData,
            },
          ],
        }
      );
    }
  }

  // ------------------------------------------------------------------ //
  // 告警铃铛 + 横幅 + 下拉面板
  // ------------------------------------------------------------------ //
  var lastShownAlertId = 0; // 横幅去重游标（仅内存）
  var latestAlerts = [];    // 最近告警缓存（供面板展示）

  function getSeenId() {
    var v = parseInt(localStorage.getItem(ALERT_SEEN_KEY) || '0', 10);
    return isNaN(v) ? 0 : v;
  }

  function setSeenId(id) {
    try {
      localStorage.setItem(ALERT_SEEN_KEY, String(id));
    } catch (e) { /* ignore */ }
  }

  function unreadCount() {
    var seen = getSeenId();
    return latestAlerts.reduce(function (n, a) {
      return n + (a.id > seen ? 1 : 0);
    }, 0);
  }

  function updateBadges() {
    var unread = unreadCount();
    document.querySelectorAll('.alert-badge').forEach(function (b) {
      b.textContent = unread;
      b.classList.toggle('hidden', unread === 0);
    });
    return unread;
  }

  function markAlertsRead() {
    var maxId = latestAlerts.reduce(function (m, a) {
      return Math.max(m, a.id);
    }, 0);
    if (maxId > getSeenId()) setSeenId(maxId);
    updateBadges();
  }

  function renderAlertPanel() {
    var list = $('alert-panel-list');
    var empty = $('alert-panel-empty');
    if (!latestAlerts.length) {
      list.innerHTML = '';
      list.classList.add('hidden');
      empty.classList.remove('hidden');
      return;
    }
    list.classList.remove('hidden');
    empty.classList.add('hidden');
    list.innerHTML = '';
    latestAlerts.forEach(function (a) {
      var item = el('div', 'alert-item');
      var top = el('div', 'alert-item-top');
      var tag = el('span', 'tag tag-' + (a.ill_type || 'unknown'), illLabel(a.ill_type));
      top.appendChild(tag);
      top.appendChild(el('span', 'alert-item-rule', a.rule_name));
      top.appendChild(el('span', 'alert-item-count', 'x' + a.count));
      top.appendChild(el('span', 'alert-item-time', fmtTime(a.ts)));
      item.appendChild(top);
      item.appendChild(
        el('div', 'alert-item-meta', a.instance + (a.model ? ' · ' + a.model : ''))
      );
      list.appendChild(item);
    });
  }

  function openAlertPanel() {
    $('alert-banner').classList.add('hidden');
    markAlertsRead();
    renderAlertPanel();
    $('alert-panel').classList.remove('hidden');
  }

  function closeAlertPanel() {
    $('alert-panel').classList.add('hidden');
  }

  function toggleAlertPanel() {
    if ($('alert-panel').classList.contains('hidden')) {
      openAlertPanel();
    } else {
      closeAlertPanel();
    }
  }

  function renderBell(alerts, summary) {
    latestAlerts = (alerts || []).slice();
    // 面板打开时视为已读（用户已在看）
    if (!$('alert-panel').classList.contains('hidden')) {
      markAlertsRead();
    }
    updateBadges();
    renderAlertPanel();

    // 横幅：仅对「未展示且未读」的新告警弹一次
    var floor = Math.max(lastShownAlertId, getSeenId());
    var newest = null;
    (alerts || []).forEach(function (a) {
      if (a.id > floor && (!newest || a.id > newest.id)) newest = a;
    });
    if (newest) {
      lastShownAlertId = newest.id;
      showBanner(newest);
    }

    // 顶栏实例状态
    var inst = (summary && summary.instances) || {};
    $('instance-status').textContent =
      '在线 ' + (inst.online || 0) + ' · 离线 ' + (inst.offline || 0) + ' · 暂停 ' + (inst.paused || 0);
  }

  function showBanner(alert) {
    var b = $('alert-banner');
    b.innerHTML =
      '<strong>告警</strong>' +
      alert.rule_name +
      ' · ' +
      alert.instance +
      ' · ' +
      illLabel(alert.ill_type) +
      ' · 窗口计数 ' +
      alert.count +
      ' <span class="banner-hint">点击查看</span>';
    b.classList.remove('hidden');
    clearTimeout(showBanner._t);
    showBanner._t = setTimeout(function () {
      b.classList.add('hidden');
    }, BANNER_MS);
  }

  // ------------------------------------------------------------------ //
  // 实例管理操作
  // ------------------------------------------------------------------ //
  async function addInstance() {
    var name = $('inst-name').value.trim();
    var url = $('inst-url').value.trim();
    $('add-instance-msg').textContent = '';
    if (!name || !url) {
      $('add-instance-msg').textContent = '请填写实例名和 URL';
      return;
    }
    var r = await api('/api/instances', { method: 'POST', body: { name: name, url: url } });
    if (r.ok) {
      $('inst-name').value = '';
      $('inst-url').value = '';
      $('add-instance-msg').textContent = '';
      showToast('实例已添加: ' + name);
      refreshInstances();
    } else if (r.status === 409) {
      $('add-instance-msg').textContent = '实例名已存在';
    } else {
      var msg = r.data && r.data.detail ? r.data.detail : '添加失败';
      $('add-instance-msg').textContent = msg;
    }
  }

  async function togglePause(inst, currentlyPaused) {
    var action = currentlyPaused ? 'resume' : 'pause';
    var r = await api('/api/instances/' + encodeURIComponent(inst.name) + '/' + action, { method: 'POST' });
    if (r.ok) {
      showToast(currentlyPaused ? '已恢复: ' + inst.name : '已暂停: ' + inst.name);
      refreshInstances();
    } else {
      var msg = r.data && r.data.detail ? r.data.detail : '操作失败';
      showToast(msg, true);
    }
  }

  async function deleteInstance(inst) {
    var ok = window.confirm(
      '确认删除实例 ' +
        inst.name +
        '？\n删除将清除该实例的事件 / 告警 / 趋势 / 全局累计统计数据（不可恢复）。'
    );
    if (!ok) return;
    var r = await api('/api/instances/' + encodeURIComponent(inst.name), { method: 'DELETE' });
    if (r.ok || r.status === 204) {
      showToast('实例已删除: ' + inst.name);
      if (currentInstance === inst.name) {
        closeDetail();
        return;
      }
      refreshNow();
    } else {
      var msg = r.data && r.data.detail ? r.data.detail : '删除失败';
      showToast(msg, true);
    }
  }

  // ------------------------------------------------------------------ //
  // 历史数据导入（异常 pickle → 阶梯趋势）
  // ------------------------------------------------------------------ //
  async function populateImportInstances() {
    var sel = $('import-instance');
    sel.innerHTML = '';
    var r = await api('/api/instances');
    if (r.ok && r.data) {
      r.data.forEach(function (inst) {
        var opt = el('option', '', inst.name);
        opt.value = inst.name;
        sel.appendChild(opt);
      });
    }
    if (!sel.options.length) {
      var opt = el('option', '', '（暂无实例，可手输）');
      opt.value = '';
      sel.appendChild(opt);
    }
  }

  function toggleImportForm(show) {
    var form = $('import-form');
    if (show) {
      populateImportInstances();
      form.classList.remove('hidden');
    } else {
      form.classList.add('hidden');
      $('import-file').value = '';
    }
  }

  function toggleDetailImportForm(show) {
    var form = $('detail-import-form');
    if (show) {
      $('detail-import-instance-name').textContent = currentInstance || '-';
      form.classList.remove('hidden');
    } else {
      form.classList.add('hidden');
      $('detail-import-file').value = '';
    }
  }

  async function uploadImport(file, instance) {
    if (!file) {
      showToast('请选择文件', true);
      return;
    }
    if (!instance) {
      showToast('请指定实例名', true);
      return;
    }
    var headers = authHeaders({});
    delete headers['Content-Type'];
    var resp;
    try {
      resp = await fetch(
        '/api/import?instance=' + encodeURIComponent(instance),
        { method: 'POST', headers: headers, body: file }
      );
    } catch (e) {
      showToast('导入失败：网络错误', true);
      return;
    }
    if (resp.status === 401) {
      showLogin();
      return;
    }
    var data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (resp.ok && data) {
      showToast(
        '导入成功：' + data.imported + ' 条' +
        (data.skipped ? '（跳过 ' + data.skipped + '）' : '') +
        (data.cleared ? '，覆盖旧 ' + data.cleared + ' 条' : '')
      );
      refreshNow();
    } else {
      var msg = (data && data.detail) || '导入失败';
      showToast(msg, true);
    }
  }

  async function confirmImport() {
    var file = $('import-file').files[0];
    var instance = $('import-instance').value;
    await uploadImport(file, instance);
    if (file && instance) toggleImportForm(false);
  }

  async function confirmDetailImport() {
    var file = $('detail-import-file').files[0];
    var instance = currentInstance;
    await uploadImport(file, instance);
    if (file && instance) toggleDetailImportForm(false);
  }

  // ------------------------------------------------------------------ //
  // 轮询
  // ------------------------------------------------------------------ //
  var polling = false;
  var pollTimer = null;
  var currentWindow = '1day';
  var inFlight = false;

  function startPolling() {
    if (polling) return;
    polling = true;
    pollTimer = setInterval(poll, POLL_MS);
  }

  function stopPolling() {
    polling = false;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function poll() {
    if (inFlight || !getToken()) return;
    inFlight = true;
    try {
      if (currentInstance) {
        await pollDetail();
      } else {
        await pollDashboard();
      }
    } finally {
      inFlight = false;
    }
  }

  async function pollDashboard() {
    var summaryP = api('/api/summary');
    var instancesP = api('/api/instances');
    var alertsP = api('/api/alerts?limit=50');
    var trendsP = api('/api/trends?window=' + currentWindow);

    var results = await Promise.all([summaryP, instancesP, alertsP, trendsP]);
    var summary = results[0];
    var instances = results[1];
    var alerts = results[2];
    var trends = results[3];

    if (summary.ok && summary.data) renderSummary(summary.data);
    if (instances.ok && instances.data) renderInstances(instances.data);
    if (alerts.ok && alerts.data && summary.ok && summary.data) {
      renderBell(alerts.data, summary.data);
    }
    if (trends.ok && trends.data) {
      renderTrend(trendChart, $('trend-chart'), $('trend-empty'), trends.data.points);
    }
  }

  async function pollDetail() {
    var name = encodeURIComponent(currentInstance);
    var summaryP = api('/api/instances/' + name + '/summary');
    var trendsP = api('/api/instances/' + name + '/trends?window=' + detailWindow);

    var results = await Promise.all([summaryP, trendsP]);
    var summary = results[0];
    var trends = results[1];

    if (summary.ok && summary.data) renderDetailSummary(summary.data);
    if (trends.ok && trends.data) {
      renderTrend(detailTrendChart, $('detail-trend-chart'), $('detail-trend-empty'), trends.data.points);
    }
  }

  function refreshNow() {
    poll();
  }

  function refreshInstances() {
    api('/api/instances').then(function (r) {
      if (r.ok && r.data) renderInstances(r.data);
    });
  }

  // ------------------------------------------------------------------ //
  // 初始化
  // ------------------------------------------------------------------ //
  function bindSeg(segId, setter) {
    var seg = $(segId);
    seg.querySelectorAll('.seg-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        seg.querySelectorAll('.seg-btn').forEach(function (b) {
          b.classList.remove('active');
        });
        btn.classList.add('active');
        setter(btn.getAttribute('data-window'));
        refreshNow();
      });
    });
  }

  function bindEvents() {
    $('login-form').addEventListener('submit', handleLogin);
    $('logout-btn').addEventListener('click', logout);
    $('detail-logout-btn').addEventListener('click', logout);

    // 导航：返回看板
    $('detail-back-btn').addEventListener('click', closeDetail);

    // 告警铃铛：点击展开 / 收起面板
    $('bell').addEventListener('click', function (e) {
      e.stopPropagation();
      toggleAlertPanel();
    });
    $('detail-bell').addEventListener('click', function (e) {
      e.stopPropagation();
      toggleAlertPanel();
    });
    $('alert-clear-btn').addEventListener('click', function (e) {
      e.stopPropagation();
      markAlertsRead();
    });
    // 点击面板外部关闭
    document.addEventListener('click', function (e) {
      var p = $('alert-panel');
      if (p.classList.contains('hidden')) return;
      if (e.target.closest('#alert-panel') || e.target.closest('.bell')) return;
      closeAlertPanel();
    });

    $('add-instance-btn').addEventListener('click', addInstance);
    $('inst-url').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') addInstance();
    });

    bindSeg('window-seg', function (w) {
      currentWindow = w;
    });
    bindSeg('detail-window-seg', function (w) {
      detailWindow = w;
    });

    // 历史导入
    $('import-btn').addEventListener('click', function () {
      toggleImportForm($('import-form').classList.contains('hidden'));
    });
    $('import-cancel').addEventListener('click', function () {
      toggleImportForm(false);
    });
    $('import-confirm').addEventListener('click', confirmImport);
    $('detail-import-btn').addEventListener('click', function () {
      toggleDetailImportForm($('detail-import-form').classList.contains('hidden'));
    });
    $('detail-import-cancel').addEventListener('click', function () {
      toggleDetailImportForm(false);
    });
    $('detail-import-confirm').addEventListener('click', confirmDetailImport);

    // 点击横幅 → 展开告警面板
    $('alert-banner').addEventListener('click', openAlertPanel);
  }

  function boot() {
    bindEvents();
    initCharts();
    if (getToken()) {
      // 已有 token：先尝试拉一次 summary 验证有效性
      api('/api/summary').then(function (r) {
        if (r.ok && r.data) {
          showDashboard(localStorage.getItem(USER_KEY) || '');
          refreshNow();
        } else {
          showLogin();
        }
      });
    } else {
      showLogin();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
