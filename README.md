# 联合上门服务的成本指标与拨付核对

街道购买联合上门服务的月结资料。金额以分为单位。共同成本源由出资凭证 evidence_id 确认；provider 是收付款往来的承接方。单位量是按组合签收的服务单位，取消记录不产生单位。确认记录有自己的时间，月份按上海时区。

资料均为合成记录，采用 UTF-8 编码。交换字段、标识和状态见[资料说明](docs/data.md)。

项目使用 Python 3.11.2，资料查看与检测只依赖标准库。在项目目录运行以下命令，可查看各集合的记录数，并检查字段、重复键与跨表引用。

```sh
python3 inspect_records.py
python3 -m unittest discover -s tests -v
```

`reconcile.py` 在同一交换约定上完成月结核算与拨付核对：凭证去重、按发生期选版的整数分摊、冲销跟随原路由、可下钻到原凭证的单位成本、共同池额度占用与签发判断、待分摊与历史时点复核。

```sh
python3 reconcile.py                 # 全期文本报告
python3 reconcile.py --month 2026-05 # 单月成本口径收口
python3 reconcile.py --as-of 2026-06-10   # 按当时事实复核可签发状态
python3 reconcile.py --json          # 机器可读的对账结果
```
