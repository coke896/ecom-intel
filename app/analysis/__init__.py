"""AI 分析层：fingerprint（缓存键）、payload（装配输入）、prompts、glm_client、reporter。

依赖方向单向：payload → prompts → glm_client → reporter，没有任何一环回头依赖
reporter。因此 `build_payload()` 与 `build_fingerprint()` 是纯函数（同样的库状态
→ 同样的输出），可脱离网络和数据库单独测——指纹一旦算得不稳，缓存就会静默失效。
"""
