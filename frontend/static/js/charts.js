/* 图表渲染。每个 render 函数遵循同一套约定：
 *   renderXxx(elId, data)：data 带 notice → 显示提示条；数据为空 → 显示空态；否则正常画图。
 * "空"和"有提示"分开处理：完全没数据时提示条就是唯一内容（否则一块空白像坏了），
 * 有数据但需说明时图照画、提示当注解。 */

/* ECharts 实例登记表：容器宽度变化时 ECharts 不会自己重画，必须显式 resize()；
   登记所有实例 + 一个全局 resize 监听，一处解决。 */
const _charts = new Map();

function _getChart(elId) {
  const el = document.getElementById(elId);
  if (!el) return null;
  let chart = _charts.get(elId);
  // 容器尺寸变了（比如从空态切回有数据）时，旧实例的尺寸是过期的，resize() 要在 setOption 前调用。
  if (!chart) {
    chart = echarts.init(el);
    _charts.set(elId, chart);
  }
  return chart;
}

window.addEventListener('resize', () => {
  for (const chart of _charts.values()) chart.resize();
});

/* 配色。刻意只用 6 个颜色循环：颜色多于 6 种之后人眼分不清，
   饼图上"这两个扇区好像一个颜色"还不如不画。 */
const PALETTE = ['#2563eb', '#0891b2', '#7c3aed', '#db2777', '#ea580c', '#65a30d'];

const AXIS_STYLE = {
  axisLine: { lineStyle: { color: '#e4e7ec' } },
  axisLabel: { color: '#6b7280' },
  splitLine: { lineStyle: { color: '#f1f3f6' } },
};

/* 空态 / 提示 */

function showNotice(elId, text, kind = 'warn') {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!text) {
    el.classList.remove('show');
    el.textContent = '';
    return;
  }
  el.textContent = text;
  el.classList.toggle('info', kind === 'info');
  el.classList.add('show');
}

/** 在"显示画布"和"显示空态"之间切换。两个都要显式设置，
 *  只 show 一个会让空态文字和旧图表叠在一起。 */
function toggleEmpty(chartElId, emptyElId, isEmpty, message) {
  const chartEl = document.getElementById(chartElId);
  const emptyEl = document.getElementById(emptyElId);
  if (chartEl) chartEl.style.display = isEmpty ? 'none' : '';
  if (emptyEl) {
    if (isEmpty && message) emptyEl.textContent = message;
    emptyEl.classList.toggle('show', isEmpty);
  }
  // 从空态切回有数据时必须 resize：容器此前 display:none、宽高为 0，ECharts 记下的尺寸是 0。
  if (!isEmpty) {
    const chart = _charts.get(chartElId);
    if (chart) chart.resize();
  }
}

/* 图表 1：价格分布 */

function renderPriceDistribution(elId, emptyId, noticeId, data) {
  // 提醒的颜色按"图画没画出来"分：图空 → 黄色告警；图画出来了只是有几个商品没价格 → 蓝色说明。
  // 把后者也涂成黄色等于为一个 2% 的偏差拉警报——告警变成噪音，真正该看的就没人看了。
  //   判断依据就是"有没有柱子"，不需要后端再多返回一个字段。
  const hasBars = !!(data.buckets && data.buckets.length);
  showNotice(noticeId, data.notice, hasBars ? 'info' : 'warn');

  if (!hasBars) {
    toggleEmpty(elId, emptyId, true, data.notice || '暂无价格数据');
    return;
  }
  toggleEmpty(elId, emptyId, false);

  const chart = _getChart(elId);
  if (!chart) return;

  chart.setOption({
    grid: { left: 52, right: 20, top: 24, bottom: 46 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = ps[0];
        const b = data.buckets[p.dataIndex];
        const pct = ((b.count / data.total) * 100).toFixed(1);
        return `价格 <b>${b.label}</b> 元<br/>商品数 <b>${b.count}</b>（${pct}%）`;
      },
    },
    xAxis: {
      type: 'category',
      data: data.buckets.map((b) => b.label),
      name: '元',
      nameLocation: 'end',
      nameGap: 8,
      nameTextStyle: { color: '#9ca3af' },
      axisLabel: { ...AXIS_STYLE.axisLabel, rotate: 45 },
      axisLine: AXIS_STYLE.axisLine,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      name: '商品数',
      nameTextStyle: { color: '#9ca3af' },
      ...AXIS_STYLE,
    },
    series: [
      {
        type: 'bar',
        data: data.buckets.map((b) => b.count),
        itemStyle: { color: PALETTE[0], borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 34,
        // 桶多的时候把数值标在柱子顶上会糊成一片，超过 14 个就不标了
        label: {
          show: data.buckets.length <= 14,
          position: 'top',
          color: '#6b7280',
          fontSize: 11,
        },
      },
    ],
  }, true);
}

/* 图表 2：销量 Top20 */

function renderSalesTop(elId, emptyId, noticeId, data) {
  // 真实电商的销量不在网页端公开，?source=smzdm 下必然是空的；
  // 一条解释把"看起来像坏了"变成"已知的、有意的边界"。
  showNotice(noticeId, data.notice);

  if (!data.items || !data.items.length) {
    toggleEmpty(elId, emptyId, true, data.notice || '暂无销量数据');
    return;
  }
  toggleEmpty(elId, emptyId, false);

  const chart = _getChart(elId);
  if (!chart) return;
  // ECharts 的 y 轴类目默认从下往上排，所以要反过来喂数据，第 1 名才会在最上面。
  // 第 1 名才会出现在最上面。
  const items = [...data.items].reverse();

  chart.setOption({
    grid: { left: 150, right: 70, top: 16, bottom: 34 },
    tooltip: {
      trigger: 'item',
      // tooltip 显示完整标题：后端截断图例标签无法保证不同 SKU 长得不一样
      // （颜色后面常跟营销语），"图例只负责放得下、全文交给 tooltip"才是修法（见 app/textutil.py）。
      // 返回的字符串会被 echarts 赋给 innerHTML，必须自己转义（见 api.js 的 escapeHtml）。
      formatter: (p) => {
        const it = items[p.dataIndex];
        return [
          `<div style="max-width:340px;white-space:normal;font-weight:600">${escapeHtml(it.title)}</div>`,
          `销量：<b>${fmtInt(it.sales)}</b>`,
          `价格：${fmtPrice(it.price)}`,
          it.brand ? `品牌：${escapeHtml(it.brand)}` : null,
          it.shop ? `店铺：${escapeHtml(it.shop)}` : null,
        ].filter(Boolean).join('<br/>');
      },
    },
    xAxis: { type: 'value', ...AXIS_STYLE },
    yAxis: {
      type: 'category',
      data: items.map((it) => it.title_short),
      axisLabel: { color: '#4b5563', fontSize: 12 },
      axisLine: { lineStyle: { color: '#e4e7ec' } },
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar',
        data: items.map((it) => it.sales),
        itemStyle: {
          color: (p) => PALETTE[p.dataIndex % PALETTE.length],
          borderRadius: [0, 3, 3, 0],
        },
        barMaxWidth: 20,
        label: {
          show: true,
          position: 'right',
          color: '#6b7280',
          fontSize: 11,
          formatter: (p) => fmtInt(p.value),
        },
      },
    ],
  }, true);
}

/* 图表 3：店铺占比 */

function renderShopShare(elId, emptyId, noticeId, data) {
  showNotice(noticeId, data.notice);

  if (!data.items || !data.items.length) {
    toggleEmpty(elId, emptyId, true, data.notice || '暂无店铺数据');
    return;
  }
  toggleEmpty(elId, emptyId, false);

  const chart = _getChart(elId);
  if (!chart) return;

  chart.setOption({
    tooltip: {
      trigger: 'item',
      // 同样是 innerHTML 出口，理由见上面销量排行那条注释。
      formatter: (p) => {
        const it = data.items[p.dataIndex];
        return [
          `<b>${escapeHtml(it.shop)}</b>`,
          it.platform ? `平台：${escapeHtml(it.platform)}` : null,
          `商品数：<b>${it.product_count}</b>（${fmtPct(it.share)}）`,
          // 无价商品被排除在均价外，两者不等时必须写出分母，
          // 否则"商品数 100 ｜ 均价 ¥99"会被读成 100 件商品的均价。
          it.avg_price !== null
            ? `均价：${fmtPrice(it.avg_price)}` +
              (it.priced_count < it.product_count
                ? `<span style="color:#9ca3af">（基于 ${it.priced_count} 件有价商品）</span>`
                : '')
            : null,
        ].filter(Boolean).join('<br/>');
      },
    },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],   // 环形而不是实心饼：中间空白让外围扇区更容易比较长度
        center: ['50%', '52%'],
        data: data.items.map((it) => ({ name: it.shop, value: it.product_count })),
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        color: PALETTE,
        label: {
          formatter: '{b}\n{d}%',
          color: '#4b5563',
          fontSize: 11,
        },
        labelLine: { length: 10, length2: 10 },
        // hideOverlap 让挤在一起的外标签自动藏掉（被藏的信息仍可 hover），而不是画成一团黑。
        labelLayout: { hideOverlap: true },
        emphasis: { scale: true, scaleSize: 6 },
      },
    ],
  }, true);
}

/* 图表 4：关键词词云 */

let _wordCloudWarned = false;

function renderKeywords(elId, emptyId, noticeId, data) {
  showNotice(noticeId, data.notice);

  if (!data.items || !data.items.length) {
    toggleEmpty(elId, emptyId, true, data.notice || '暂无关键词');
    return;
  }
  toggleEmpty(elId, emptyId, false);

  const chart = _getChart(elId);
  if (!chart) return;

  const option = {
    // 词云的 name 是 jieba 从商品标题里切出来的外部数据，一样要转义。
    tooltip: { formatter: (p) => `${escapeHtml(p.name)}：出现 <b>${p.value}</b> 次` },
    series: [
      {
        type: 'wordCloud',
        shape: 'circle',
        // 尺寸范围：太小看不清、太大放不下几个词；12~48 能塞下约 60~90 个词。
        sizeRange: [12, 48],
        rotationRange: [0, 0],   // 不旋转：中文竖排/斜排几乎读不出来，纯观感取舍。
        gridSize: 6,
        drawOutOfBound: false,
        layoutAnimation: true,
        textStyle: {
          fontFamily: '"PingFang SC","Microsoft YaHei",sans-serif',
          fontWeight: 600,
          color: () => PALETTE[Math.floor(Math.random() * PALETTE.length)],
        },
        emphasis: { textStyle: { fontSize: 52 } },
        data: data.items.map((it) => ({ name: it.name, value: it.value })),
      },
    ],
  };

  try {
    chart.setOption(option, true);
  } catch (err) {
    /* try/catch 是为一个报错信息会误导人的故障准备的：echarts-wordcloud 插件没加载出来时
       ECharts 报 "series.wordCloud not exists. Load it first."——会把人引向"type 写错了"。
       （另一个坑：插件 2.1.0 只兼容 echarts ^5.0.1，升到 6 也报同一句错，所以 vendor 锁 5.6.0。） */
    if (!_wordCloudWarned) {
      _wordCloudWarned = true;
      console.error('[词云] 渲染失败，通常是 echarts-wordcloud 插件没加载出来：', err);
    }
    toggleEmpty(
      elId, emptyId, true,
      '词云插件（echarts-wordcloud）未能加载，且本地 vendor/ 里也没有副本。'
      + '检查 frontend/vendor/echarts-wordcloud.min.js 是否存在。'
    );
  }
}

/* 图表 5：单个商品的价格历史（详情弹窗里用） */

function renderPriceHistory(elId, data) {
  const chart = _getChart(elId);
  if (!chart) return;

  // 没有价格点时**必须显式清空**，不能直接 return：_getChart 按容器 id 缓存实例、
  // 详情弹窗只换内容不换容器，直接 return 会把上一件商品的曲线原样留在新标题下面冒充它。
  // （"为什么没有价格点"由详情接口的 notice 解释；这里只负责别留假曲线。）
  if (!data.points.length) {
    chart.clear();
    return;
  }

  const prices = data.points.map((p) => p.price);
  const times = data.points.map((p) => p.recorded_at.slice(5, 16));

  chart.setOption({
    grid: { left: 58, right: 24, top: 30, bottom: 54 },
    tooltip: {
      trigger: 'axis',
      formatter: (ps) => {
        const p = ps[0];
        const pt = data.points[p.dataIndex];
        return [
          fmtTime(pt.recorded_at),
          `价格：<b>${fmtPrice(pt.price)}</b>`,
          // 把 source_ref 显示出来让"事件归并"可见：每个价格点对应一篇爆料，
          // 同一商品被反复爆料就会长出多个点——这是 mock 数据给不了的证据。
          pt.source_ref ? `来源爆料：${escapeHtml(pt.source_ref)}` : null,
        ].filter(Boolean).join('<br/>');
      },
    },
    xAxis: {
      type: 'category',
      data: times,
      name: data.x_axis_label,   // 如实标注：x 轴到底是什么时间
      nameLocation: 'middle',
      nameGap: 30,
      nameTextStyle: { color: '#9ca3af' },
      axisLabel: { ...AXIS_STYLE.axisLabel, rotate: 40, fontSize: 10 },
      axisLine: AXIS_STYLE.axisLine,
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      name: '元',
      scale: true,        // 不从 0 开始：价格全在 100~130 时从 0 起画会把波动压成直线——那是图在骗人。
      nameTextStyle: { color: '#9ca3af' },
      ...AXIS_STYLE,
    },
    series: [
      {
        type: 'line',
        data: prices,
        smooth: false,      // 不做平滑：平滑会画出实际不存在的中间值，价格曲线不该这样。
        symbolSize: 4,
        lineStyle: { color: PALETTE[0], width: 2 },
        itemStyle: { color: PALETTE[0] },
        areaStyle: { color: 'rgba(37,99,235,.08)' },
        markPoint: {
          symbolSize: 42,
          label: { fontSize: 10, color: '#fff' },
          data: [
            { type: 'min', name: '最低', itemStyle: { color: '#16a34a' } },
            { type: 'max', name: '最高', itemStyle: { color: '#dc2626' } },
          ],
        },
        // 只有一个点时线画不出来，但 symbol 和 markPoint 照常显示，
        // "只被爆料过一次"在图上仍然有信息量（真实采集的常态）。
      },
    ],
  }, true);
}
