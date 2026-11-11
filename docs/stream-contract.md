# G1 状态流实现合同

日期：2026-11-09（Asia/Shanghai）。范围：本机 IPC 的已提交状态投影、断线恢复及故障行为，不接入柜台，不创建成交或交易授权。

## 服务端接口

`kernel.streams.StreamHub(kernel_boot_id, account_key)`只由内核owner线程调用。每个流的键为`(client_instance_id,subscription_id,view)`；account、boot和projection/1由hub实例固定，三个现有view均只包含已定义ACCOUNT/CYCLE/ORDER/BLOCK记录。

| 方法 | 合同 |
| --- | --- |
| update(batch,event_seq,durable_seq) | 只接受已提交边界，返回`{key:[(type,body)]}`；调用方必须按每次提交确认依次调用，不能只把最后快照交进来合并掉中间事实 |
| request(instance,SnapshotRequest.body) | 返回`(key,messages)`；完整快照为Begin/Chunk/End，保留恢复为Delta列表，失效为Gap |
| detach(key) | 连接断开，保持有界历史以便同身份重连；不删除账本、计划或资金占用 |
| invalidate(key,reason) | 发送容量/序列失效后的明确Gap，清除该流保留区间；随后必须请求完整快照 |
| last_stream_seq(key) | 当前流高水位，供心跳及重连完成屏障使用 |

新订阅完整快照的B为0，后续每条Delta从1连续递增。完整快照一次性截取已提交N、stream B及不可变canonical内容；快照生成以后到达的提交产生B+1起的Delta，不能改写正在发送的N快照。Delta含完整新记录，同record_revision异内容、修订倒退或删除历史记录会使源投影失效并产生SEQUENCE_CONFLICT；此时hub拒绝再次把最后旧投影当成“当前完整快照”，须先收到合法更高修订的已提交状态。

默认最多64个流，每流保留1,024帧/16MiB。已送出序号不静默丢弃或合并。正常保留窗口可滚动淘汰旧帧，过旧游标恢复得到RETENTION_EXPIRED；单条记录本身超过容量得到QUEUE_OVERFLOW。满流容量只允许回收断开流，活跃流满则拒绝新订阅。pipe出队容量由server另管，满时调用invalidate，不能阻塞内核等待客户端。

同boot/account/instance/view/subscription的保留游标才可恢复。boot或schema不同、游标超前、保留窗口缺失均返回明确Gap，不将另一流的序列接到旧视图。恢复Delta后server应补一条Heartbeat，其last_stream_seq等于当前高水位；即使没有新Delta，也需要该屏障让客户端确认已追平。

## 客户端接口

`transport.subscription.SubscriptionState(subscription_id,view=...)`为纯状态消费器，`bind(Welcome)`绑定当前pipe身份，`apply(envelope)`处理快照/Delta/Gap/Heartbeat。`projection()`总返回副本；`stale`和`require_snapshot`必须用于界面状态，不能只看有无缓存数据。

`LiveSubscription(pipe,client_instance_id=...,subscription_id=...)`复用上述消费器及原生管道，提供`connect(resume=True)`、`poll(timeout=...)`、`projection()`、`request_snapshot()`和`close()`。instance/subscription标识在重连中稳定；默认Gap后自动申请完整快照，仍保留旧视图并标STALE，直到完整校验通过。任何控制操作都使用独立的版本化命令，不通过Delta反向写事实。

客户端核对scope/boot/connection、snapshot_id、分块声明/长度/hash、完整canonical编码、record身份/修订、N<=durable及连续stream_seq。完整快照在最后一块与End验证后才原子替换；快照期间的增量暂存于有界缓冲，依次核对B+1后再整体发布。缺块、冲突、跨boot、超量缓冲及乱序都保留旧视图并标失效，不能发布半份结果。

断开后旧视图STALE。重连收到保留Delta时按序更新，但仍等待Heartbeat高水位确认追平；新boot只接受新的完整快照，不混入旧连接片段。最后一次Heartbeat超过15秒则标STALE；心跳只是链路存活，不能刷新“业务实际推进”时点。Heartbeat.last_event_seq可大于durable_seq以显示owner待提交观察，客户端不据它推进已提交视图或冒充持久成功；Delta仍要求event_seq<=durable_seq。

`HeartbeatAck`只回客户端最后消息/事件/流游标及自身已处理业务次数，不向内核上报权威资金/持仓，也不授予任何交易权。

## 本轮验证

独立用例覆盖原子快照、N后新事实、连续变更、保留重连、Gap/容量、身份隔离、修订冲突、乱序/重复、hash/缺块、boot切换、心跳与业务推进分离及网络包装的稳定身份。

`tests/test_stream_integration.py`使用实际Windows原生管道、本机PID绑定和独立临时账本；内核创建、控制处理和CommitAck应用均在同一owner线程。四项联调结果全部通过：

1. 收到完整SnapshotBegin/Chunk/End及Heartbeat后，用另一条控制连接提交PAUSE_NEW_ENTRIES；客户端收到连续、已持久化的ACCOUNT/BLOCK Delta，资金未知值仍为UNKNOWN/null，结果与独立拉取快照一致。
2. 稳定instance/subscription断开期间完成两次PAUSE；重连只重放保留Delta，等待Heartbeat高水位后解除STALE，服务端实际收到对应stream/event游标的HeartbeatAck。
3. 同管道、同账本启动新boot后，旧游标先得到BOOT_CHANGED Gap；旧缓存维持STALE，完整新快照校验后原子替换，流序列从新boot的B=0重新绑定。
4. 先令三笔SQLite提交全部完成，再由owner一次poll接收三个CommitAck；客户端收到连续六条Delta，ACCOUNT和BLOCK均保留三个提交各自的中间record_revision和durable_seq。

服务端用`peer.stream_ready`控制恢复屏障：Gap置false，完整快照或保留重放成功后置true，再发送即时Heartbeat；周期Heartbeat也只发往ready流。这样boot改变后的Gap不会紧跟旧游标无法接受的零高水位心跳，也不会打断客户端正在申请的完整快照。后续容量或序列失效的推送Gap同样清除ready标记。

验证命令：`.venv\Scripts\python.exe -m unittest discover -s tests -p test_stream*.py -q`，24项通过，其中4项为上述实际管道联调。未接入柜台，未创建报单或成交。
