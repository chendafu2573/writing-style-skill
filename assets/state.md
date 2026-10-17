# 写作状态

> 通用模板空白初始状态。下表与 YAML（便于程序读取的键值表）必须一致；1970-01-01 仅为初始化标记，不代表真实使用日期。

| 字段 | 类型 | 含义 | 当前值 |
|---|---|---|---|
| `polish_count_total` | 整数 | 已存档创作累计数 | 0 |
| `dna_active_count` | 整数 | 当前偏好账本条目数 | 0 |
| `dna_archive_count` | 整数 | 淘汰偏好条目数 | 0 |

```yaml
polish_count_total: 0
dna_active_count: 0
dna_archive_count: 0
last_updated: 1970-01-01
```
