# Contributing

感谢你愿意一起把「成本排雷」这件事做扎实。

## 开发环境

- Python ≥ 3.11
- 无第三方依赖（YAML 解析器为内置子集实现）
- 运行测试：

```bash
python -m unittest discover -s tests
```

## 提交前检查

1. 全部测试通过；
2. 单行不超过 100 字符；
3. 新增金额计算必须有测试覆盖；
4. AI 层不得参与金额计算，提示词必须保留「数字只能来自事实、不得编造」的约束。

## 扩展方向

- 新行业模板：复制 `config_templates/discrete_manufacturing.yaml`；
- 新 ERP 导出：在 `costcontrol/coverage.py` 的 `SCHEMAS` 注册表头，再补适配器；
- 新排雷规则：在 `costcontrol/audit.py` 增加 `DataAnomaly`，并在 `detect_anomalies` 中给出证据与动作。

## 数据与隐私

请勿提交真实企业账套、供应商价格、员工姓名或任何未脱敏数据。
演示数据全部为虚构；如需投稿真实场景，请先脱敏并说明口径。
