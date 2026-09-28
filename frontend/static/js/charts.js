/* ============================================================================
 * 图表渲染
 *
 * 每个 render 函数都遵循同一套约定：
 *
 *      renderXxx(elId, data)
 *        · 如果 data 里带了 notice → 把提示条显示出来
 *        · 如果数据是空的          → 显示空态文字，隐藏画布
 *        · 否则                    → 正常画图
 *
 * ★ 为什么"空"和"有提示"要分开处理，而不是只用一种：
 *   两种情况的文案位置不一样。
 *     · 完全没数据（?source=smzdm 的销量图）→ 提示条就是唯一内容，
 *       应该占满整个图表区，否则用户看到一块空白会以为坏了
 *     · 有数据但需要说明（店铺占比只有一个"未知店铺"）→ 图要照画，
 *       提示条挂在图上方当注解
 *   把它硬塞进一种形态，必然有一边别扭。
 * ========================================================================== */

/* ---------------------------------------------------------------------------
 * ECharts 实例登记表
 *
 * ★ 为什么要有这张表：容器宽度变化时 ECharts **不会**自己重画，
 *   必须显式调用 `chart.resize()`。没有这一步，把窗口拉宽之后
 *   图还是原来那么大，右边留一片空白。
 *   登记所有实例，再挂一个全局 resize 监听，一处解决。
 * ------------------------------------------------------------------------ */
const _charts = new Map();

function _getChart(elId) {
  const el = document.getElementById(elId);
  if (!el) return null;
  let chart = _charts.get(elId);
  // 容器尺寸变了（比如从空态切回有数据）时，旧实例的尺寸信息是过期的，
  // resize() 必须在 setOption 之前调用。
  if (!chart) {
    chart = echarts.init(el);
    _charts.set(elId, chart);
  }
  return chart;
}

window.addEventListener('resize', () => {
  for (const chart of _charts.values()) chart.resize();
});

/** 配色。
 *
 *  ★ 和后端无关，只影响观感，但刻意只用 6 个颜色循环：
 *    颜色多于 6 种之后人眼就分不清了，饼图上会出现
 *    "这两个扇区好像一个颜色"的情况，那种图还不如不画。
 */
const PALETTE = ['#2563eb', '#0891b2', '#7c3aed', '#db2777', '#ea580c', '#65a30d'];

const AXIS_STYLE = {
  axisLine: { lineStyle: { color: '#e4e7ec' } },
  axisLabel: { color: '#6b7280' },
  splitLine: { lineStyle: { color: '#f1f3f6' } },
};

/* ---------------------------------------------------------------------------
 * 空态 / 提示
 * ------------------------------------------------------------------------ */

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

/** 在"显示画布"和"显示空态"之间切换。
 *
 *  ★ 注意两个都要显式设置：只 show 一个而不 hide 另一个的话，
 *    上一次的数据还在下面留着，会出现"空态文字和旧图表叠在一起"。
 */
function toggleEmpty(chartElId, emptyElId, isEmpty, message) {
  const chartEl = document.getElementById(chartElId);
  const emptyEl = document.getElementById(emptyElId);
  if (chartEl) chartEl.style.display = isEmpty ? 'none' : '';
  if (emptyEl) {
    if (isEmpty && message) emptyEl.textContent = message;
    emptyEl.classList.toggle('show', isEmpty);
  }
  // 从空态切回有数据时，容器此前是 display:none（宽高为 0），
  // ECharts 记下的尺寸是 0——必须 resize 一次才能正确绘制。
  if (!isEmpty) {
    const chart = _charts.get(chartElId);
    if (chart) chart.resize();
  }
}

/* ---------------------------------------------------------------------------
 * 图表 1：价格分布
 * ------------------------------------------------------------------------ */

function renderPriceDistribution(elId, emptyId, noticeId, data) {
  showNotice(noticeId, data.notice);

  if (!data.buckets || !data.buckets.length) {
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

/* ---------------------------------------------------------------------------
 * 图表 2：销量 Top20
 * ------------------------------------------------------------------------ */

function renderSalesTop(elId, emptyId, noticeId, data) {
  // ★ 这里是全项目最需要"好好说人话"的地方。
  //   真实电商的销量不在网页端公开，所以 ?source=smzdm 下必然是空的。
  //   一条解释，把"看起来像坏了"变成"这是已知的、有意的边界"。
  showNotice(noticeId, data.notice);

  if (!data.items || !data.items.length) {
    toggleEmpty(elId, emptyId, true, data.notice || '暂无销量数据');
    return;
  }
  toggleEmpty(elId, emptyId, false);

  const chart = _getChart(elId);
  if (!chart) return;

  // ECharts 的 y 轴类目默认是**从下往上**排的，所以要反过来喂数据，
  // 第 1 名才会出现在最上面。
  const items = [...data.items].reverse();

  chart.setOption({
    grid: { left: 150, right: 70, top: 16, bottom: 34 },
    tooltip: {
      trigger: 'item',
      // ★ tooltip 里显示**完整标题**，而不是图例上那个被截断的。
      //
      //   这不是可有可无的装饰：后端截断图例标签时，无论怎么截
      //   都不能保证不同的 SKU 一定长得不一样（颜色后面常常还跟着
      //   "商务款/送礼佳品"这类营销语）。所以"图例只负责放得下，
      //   全文交给 tooltip"是这个问题真正的修法。
      //   见 app/textutil.py 的模块注释。
      // ★ 返回的字符串**会被 echarts 赋给 innerHTML**，所以要自己转义。
      //   这几个字段全部来自采集数据（标题、品牌、店铺），
      //   而"文本填进 DOM 用 textContent"这条纪律管不到这里——
      //   详见 api.js 里 escapeHtml 上方那段注释。
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

/* ---------------------------------------------------------------------------
 * 图表 3：店铺占比
 * ------------------------------------------------------------------------ */

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
      // ★ 同样是 innerHTML 出口，理由见上面销量排行那条注释。
      formatter: (p) => {
        const it = data.items[p.dataIndex];
        return [
          `<b>${escapeHtml(it.shop)}</b>`,
          it.platform ? `平台：${escapeHtml(it.platform)}` : null,
          `商品数：<b>${it.product_count}</b>（${fmtPct(it.share)}）`,
          it.avg_price !== null ? `均价：${fmtPrice(it.avg_price)}` : null,
        ].filter(Boolean).join('<br/>');
      },
    },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],   // 环形而不是实心饼：中间那块空白
                                  // 反而让外围的扇区更容易比较长度
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
        // ★ 扇区多的时候外标签必然互相压。hideOverlap 让 ECharts
        //   自动藏掉挤在一起的标签，而不是画成一团黑。
        //   （被藏掉的信息仍然可以 hover 出来。）
        labelLayout: { hideOverlap: true },
        emphasis: { scale: true, scaleSize: 6 },
      },
    ],
  }, true);
}

/* ---------------------------------------------------------------------------
 * 图表 4：关键词词云
 * ------------------------------------------------------------------------ */

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
    // ★ 词云的 name 是 jieba 从商品标题里切出来的，仍然属于外部数据，
    //   一样要转义（理由同销量排行的 tooltip）。
    tooltip: { formatter: (p) => `${escapeHtml(p.name)}：出现 <b>${p.value}</b> 次` },
    series: [
      {
        type: 'wordCloud',
        shape: 'circle',
        // 尺寸范围：太小看不清，太大放不下几个词。
        // 12~48 在 340px 高的容器里大约能塞下 60~90 个词。
        sizeRange: [12, 48],
        rotationRange: [0, 0],   // 不旋转。中文竖排/斜排在词云里几乎读不出来，
                                 // 而且会让整体看起来很乱——这是纯观感取舍。
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
    /* ★ 这个 try/catch 是为一个**特定的、报错信息会误导人的**故障准备的。
     *
     *   echarts-wordcloud 插件加载失败（CDN 挂 + 本地 vendor 也没下到）时，
     *   ECharts 抛的是：
     *       "series.wordCloud not exists. Load it first."
     *   这句话会把人引向"是不是我 type 写错了 / 是不是要 import"，
     *   而真实原因是插件脚本压根没到位。
     *
     *   所以这里把它翻译成一句能直接行动的话。
     *   （另一个已知的坑：插件 2.1.0 只兼容 echarts ^5.0.1，
     *     升到 ECharts 6 也会报同一句错。所以本地 vendor 锁的是 5.6.0。）
     */
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

/* ---------------------------------------------------------------------------
 * 图表 5：单个商品的价格历史（详情弹窗里用）
 * ------------------------------------------------------------------------ */

function renderPriceHistory(elId, data) {
  const chart = _getChart(elId);
  if (!chart) return;

  // ★ 没有价格点时**必须显式清空**，不能直接 return。
  //
  //   _getChart 是按容器 id 缓存实例的，而详情弹窗只换内容、不换容器——
  //   所以关掉弹窗再打开另一件商品时，画布不会自己重置。
  //   直接 return 的话，上一件商品的曲线会**原样留在新商品的标题下面**，
  //   看起来就是"这件商品的价格历史"，而它是假的。
  //
  //   真实数据里马上能撞到：id=1023、id=1098 这两条 smzdm 商品的价格点数为 0，
  //   而其余 98 条都 ≥1 点。先开有点的、再开这两条，就会看到残留。
  //
  //   "为什么没有价格点"由详情接口的 notice 负责解释（弹窗里 m-notice 那块），
  //   这里只负责一件事：**别把上一条曲线留着冒充这一条的。**
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
          // ★ 把 source_ref 显示出来，是为了让"事件归并"这件事可见：
          //   每个价格点对应一篇爆料文章，同一个商品被反复爆料，
          //   这里就会长出多个点。这是 mock 数据给不了的证据。
          pt.source_ref ? `来源爆料：${escapeHtml(pt.source_ref)}` : null,
        ].filter(Boolean).join('<br/>');
      },
    },
    xAxis: {
      type: 'category',
      data: times,
      name: data.x_axis_label,   // ★ 如实标注：x 轴到底是什么时间
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
      scale: true,        // ★ 不从这个 0 开始。价格全在 100~130 之间时，
                          //   从 0 起画会把 30 元的波动压成一条直线，
                          //   看起来"价格就没变过"——那是图在骗人。
      nameTextStyle: { color: '#9ca3af' },
      ...AXIS_STYLE,
    },
    series: [
      {
        type: 'line',
        data: prices,
        smooth: false,      // 不做平滑：平滑会在两个数据点之间
                            // 画出实际不存在的中间值，价格曲线尤其不该这样
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
        // 只有一个点时（真实采集的常态）线画不出来，
        // 但 symbol 还看得见。markPoint 也照常显示，
        // 于是"这个商品只被爆料过一次"在图上仍然有信息量。
      },
    ],
  }, true);
}
