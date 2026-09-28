"""AI 分析层。

    fingerprint.py   数据指纹（缓存键）
    payload.py       把库里的数据装配成"喂给模型的那一份"
    prompts.py       prompt 模板
    glm_client.py    GLM 调用封装（重试 + JSON 解析）
    reporter.py      报告生成 + 指纹缓存

★ 这四个文件的依赖方向是**单向**的：

    payload ──▶ fingerprint
       │            │
       ▼            ▼
    prompts ──▶ glm_client ──▶ reporter

  没有任何一环回头依赖 reporter。这样 `build_payload()` 和
  `build_fingerprint()` 都是**纯函数**（同样的库状态 → 同样的输出），
  可以脱离网络和数据库单独测——而这两个函数恰恰是最需要被钉死的：
  指纹一旦算得不稳定，缓存就会静默失效（见 fingerprint.py 的模块注释）。
"""
