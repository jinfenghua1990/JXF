# 统一离线检查

在 backend 目录执行：

```bash
python -m tests.run_checks
```

分别运行旧版自检和新增回归：

```bash
python -m tests.run_checks --group selfcheck
python -m tests.run_checks --group regression
```

每组使用独立子进程、临时数据库和临时上传目录，覆盖继承来的数据库及旺店通配置，不连接生产数据库。临时文件结束后清理。任意一组失败，统一入口返回非零退出码；不会将未执行的测试报告为通过。

- selfcheck：账号密码、元数据、新建核验、沟通轮次、责任改判、金额、统计、旧表导入、筛选导出、账号管理。
- regression：单号修改与重复、金额边界、登记时间与月份边界、入库匹配、退回判定、商品快照、双方沟通、报表回填等 38 项回归检查。

离线测试通过测试进程内的旺店通模拟响应运行，生产仍要求旺店通返回原始订单号后才允许新建登记。模拟核验成功与失败均有检查。已赔付需要实际实收金额，不再把状态变更当作到账凭据。

回复轮次 warehouse / owner / warehouse = 1 / 1 / 2 保持原设计。新登记售后原因提供四种业务分类；wrong 和 other 作为历史兼容值保留，不重新加入新建下拉框。

这两个测试模块按显式脚本方式运行，统一入口负责进程隔离；不要在同一个解释器里合并导入执行。页面交互检查与真实旺店通联网抽查不计入本入口的离线通过项。

NAS 使用已发布镜像的完整离线检查：

```bash
sudo /usr/local/bin/docker run --rm --network none --entrypoint python claims-system:local -m tests.run_checks
```

不需要给测试容器挂载业务卷、生产 .env 或数据库配置。
