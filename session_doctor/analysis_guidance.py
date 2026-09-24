"""Short, attributed review criteria; never substitute them for task evidence."""

VERSION = '2026-09-24'

GUIDANCE = [
    {
        'id': 'goal_scope',
        'title': 'OpenAI：精简 Skill、AGENTS.md 与工作流程',
        'url': 'https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra',
        'principle': '按当前任务加载指导和验证；将新增步骤与需求关联。用户要求精简时仍须保留生产目标，避免在过度设计与过度简化之间摆动。',
    },
    {
        'id': 'minimal_design',
        'title': 'Martin Fowler：YAGNI',
        'url': 'https://martinfowler.com/bliki/Yagni.html',
        'principle': '审查为假定未来需求提前建设的功能和抽象，比较构建、延迟、维护与撤除代价。保持代码可修改、必要测试和已有明确需求不属于多余设计。',
    },
    {
        'id': 'useful_work',
        'title': 'Google SRE：衡量并减少重复劳动',
        'url': 'https://sre.google/workbook/eliminating-toil/',
        'principle': '先确认重复、无新增价值的工作及其实际代价，再比较修复成本与长期收益。优先消除产生重复劳动的原因；首次探索和有效修复不直接判为浪费。',
    },
    {
        'id': 'measured_latency',
        'title': 'OpenAI：延迟优化',
        'url': 'https://developers.openai.com/api/docs/guides/latency-optimization',
        'principle': '区分模型生成、请求往返和工具等待；减少无价值往返，有独立工作时再考虑并行。不能从长上下文或空白时间直接推断具体延迟根因。',
    },
    {
        'id': 'cache_policy',
        'title': 'OpenAI：Prompt caching',
        'url': 'https://developers.openai.com/api/docs/guides/prompt-caching',
        'principle': '分别看非缓存输入、缓存输入与输出；前缀变化、模型变化、压缩及间隔影响复用。同一会话不保证缓存命中，低命中率本身不证明配置故障。',
    },
    {
        'id': 'risk_based_controls',
        'title': 'OWASP：以风险和业务约束选择安全控制',
        'url': 'https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html',
        'principle': '把安全控制映射到真实资产、威胁、业务影响及约束，比较缓解成本和替代方案。既不能因出现安全设计就判多余，也不能没有依据地扩大认证与审批前置。',
    },
]
