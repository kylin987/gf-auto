# 闲鱼客户端状态检测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在闲鱼客户端新增独立“状态检测”菜单，通过一次只读检测发现本机环境、闲鱼登录、Gateway 执行器、双机冲突、任务失败和商品管理能力问题。

**Architecture:** 客户端新增独立检测器，读取 `XianyuLive`、`GatewayClient` 和 Outbox 的只读状态，并调用 Gateway 鉴权诊断接口。Gateway 在实时 session 中保存连接实际 `storeId/instanceId`，按 Token 当前授权范围查询在线设备、执行器和近期任务，不调用 Bus 业务接口、不新增表。

**Tech Stack:** Python 3、Tkinter、requests、websockets、SQLite；PHP 8.1、Webman、GatewayWorker、Illuminate Database。

**Spec:** `docs/superpowers/specs/2026-09-11-status-diagnostics-design.md`

## Global Constraints

- 状态检测只在用户点击“立即检测”时运行，不做后台巡检或主动告警。
- 检测过程只读，不自动重连、清 Cookie、打开 Chrome 或修改业务状态。
- Gateway 和客户端统一使用成功码 `200`。
- Gateway 诊断严格限定当前 Token 的 `businessType=xianyu`、`platform=fish`、`pubId` 和 `storeIds`。
- 不修改 `yhs-bus`，不新增数据库表，不新增 Bus RPC。
- 不提交各仓库中与本功能无关的现有修改和未跟踪文件。

---

### Task 1: Gateway 实时连接 presence 与诊断规则

**Files:**
- Create: `/Users/wind/wwwroot/yhs-plugin-gateway/app/common/library/ClientDiagnosticsPolicy.php`
- Modify: `/Users/wind/wwwroot/yhs-plugin-gateway/app/common/server/WsMessageServer.php`
- Create: `/Users/wind/wwwroot/yhs-plugin-gateway/tests/client_diagnostics_policy_test.php`

**Interfaces:**
- Consumes: `client.bind.payload.storeId`、`client.bind.payload.instanceId`、Token claims 和 Gateway session。
- Produces: `ClientDiagnosticsPolicy::presence(array $claims, array $payload): array`、`summarizeSessions(array $sessions, int $pubId, int $storeId): array`、`executorOwned(array $executor, array $executorSession, array $claims, array $instance): bool`。

- [ ] **Step 1: 编写失败测试，固定 presence 校验和双设备去重规则**

```php
$presence = ClientDiagnosticsPolicy::presence($claims, [
    'storeId' => 203,
    'instanceId' => 'store-a',
]);
assert($presence === ['storeId' => 203, 'instanceId' => 'store-a']);

$summary = ClientDiagnosticsPolicy::summarizeSessions($sessions, 3, 203);
assert($summary['connectionCount'] === 3);
assert($summary['deviceCount'] === 2);
assert($summary['deviceIds'] === ['device-a', 'device-b']);
```

测试还必须覆盖：非授权 `storeId` 得到空 presence、其他 `pubId` 被过滤、缺少 `instanceId` 不计入有效店铺连接、同设备两条连接只计一台。

- [ ] **Step 2: 运行测试并确认失败**

Run: `php tests/client_diagnostics_policy_test.php`

Expected: FAIL，提示 `ClientDiagnosticsPolicy` 不存在。

- [ ] **Step 3: 实现最小纯规则类**

```php
final class ClientDiagnosticsPolicy
{
    public static function presence(array $claims, array $payload): array;

    public static function summarizeSessions(
        array $sessions,
        int $pubId,
        int $storeId
    ): array;

    public static function executorOwned(
        array $executor,
        array $executorSession,
        array $claims,
        array $instance
    ): bool;
}
```

`summarizeSessions()` 只信任 session 内已经验证的 `claims` 和经过校验的 `presence`。`executorOwned()` 必须验证 `pubId + storeId + instanceId + deviceId`、`chromeLoggedIn`、租约有效和实时在线标记。

- [ ] **Step 4: 在 bind session 中保存 presence**

`WsMessageServer::bind()` 调用规则类生成 presence，并加入 `Gateway::setSession()`：

```php
Gateway::setSession($clientId, [
    'claims' => $claims,
    'presence' => ClientDiagnosticsPolicy::presence($claims, $payload),
    'uid' => PluginGroups::uid($claims),
    'pubGroup' => PluginGroups::pubGroup($claims),
    'storeGroups' => $storeGroups,
]);
```

为兼容旧客户端，presence 无效时 bind 仍可成功，但该连接不参与精确店铺诊断。

- [ ] **Step 5: 运行规则测试和语法检查**

Run: `php tests/client_diagnostics_policy_test.php && php -l app/common/library/ClientDiagnosticsPolicy.php && php -l app/common/server/WsMessageServer.php`

Expected: 所有断言通过，两个文件均显示 `No syntax errors detected`。

- [ ] **Step 6: 提交 Task 1**

```bash
git add app/common/library/ClientDiagnosticsPolicy.php app/common/server/WsMessageServer.php tests/client_diagnostics_policy_test.php
git commit -m "feat: track xianyu client presence"
```

---

### Task 2: Gateway 客户端诊断接口

**Files:**
- Create: `/Users/wind/wwwroot/yhs-plugin-gateway/app/client/validate/ClientDiagnosticsValidate.php`
- Create: `/Users/wind/wwwroot/yhs-plugin-gateway/app/client/controller/v1/ClientDiagnosticsController.php`
- Create: `/Users/wind/wwwroot/yhs-plugin-gateway/app/common/server/ClientDiagnosticsServer.php`
- Modify: `/Users/wind/wwwroot/yhs-plugin-gateway/app/client/route/index.php`
- Modify: `/Users/wind/wwwroot/yhs-plugin-gateway/tests/client_diagnostics_policy_test.php`

**Interfaces:**
- Consumes: `POST /api/v1/client/diagnostics`，Bearer Token，请求 `instances: [{storeId:int, instanceId:string}]`。
- Produces: `Show::success(['stores' => [...], 'checkedAt' => date('c')])`，HTTP/业务成功码均为 `200`；Token 无效返回 `401`。

- [ ] **Step 1: 扩充失败测试，固定授权过滤与任务结果格式**

```php
$instances = ClientDiagnosticsPolicy::authorizedInstances(
    [['storeId' => 203, 'instanceId' => 'store-a'], ['storeId' => 999, 'instanceId' => 'x']],
    ['storeIds' => [203]]
);
assert($instances === [['storeId' => 203, 'instanceId' => 'store-a']]);
```

测试必须覆盖重复店铺去重、非法实例结构丢弃、越权店铺不进入查询。

- [ ] **Step 2: 运行测试并确认新断言失败**

Run: `php tests/client_diagnostics_policy_test.php`

Expected: FAIL，提示 `authorizedInstances` 不存在。

- [ ] **Step 3: 实现验证器和薄控制器**

```php
class ClientDiagnosticsValidate extends Validate
{
    protected $rule = ['instances' => 'require|array'];
    protected $scene = ['check' => ['instances']];
}

class ClientDiagnosticsController extends BaseController
{
    public function check()
    {
        $params = request()->all();
        $this->validate($params, ClientDiagnosticsValidate::class . '.check');
        $result = (new ClientDiagnosticsServer())->check($this->bearerToken(), $params);
        // code=401 使用 HTTP 401，其余使用 HTTP 200。
    }
}
```

- [ ] **Step 4: 实现服务端查询**

`ClientDiagnosticsServer::check()` 必须：

1. 使用 `TokenServer::verify()` 并校验 xianyu/fish。
2. 通过 `authorizedInstances()` 过滤请求。
3. 使用 `Gateway::getClientSessionsByGroup()` 获取候选连接，再按 session claims 的 `pubId` 和 presence `storeId` 精确过滤。
4. 查询 `pg_plugin_fish_executors` 时同时限定 `pub_id + store_id + expires_at + chrome_logged_in=1`，并用 `Gateway::isOnline()` 验证连接。
5. 查询最近 30 分钟 `pg_plugin_tasks`，限定 `pub_id + store_id + business_type=xianyu + platform=fish` 和四类任务；从同一 `task_id` 最新 `PluginTaskResult` 读取错误原因。

```php
public function check(string $accessToken, array $params): array
{
    // 返回每店铺 authorized、connectionCount、deviceCount、executor、recentTasks。
}
```

- [ ] **Step 5: 注册路由并验证响应约定**

```php
Route::post('/diagnostics', [ClientDiagnosticsController::class, 'check']);
```

Run: `php -l app/client/validate/ClientDiagnosticsValidate.php && php -l app/client/controller/v1/ClientDiagnosticsController.php && php -l app/common/server/ClientDiagnosticsServer.php && php -l app/client/route/index.php`

Expected: 全部语法检查通过。

- [ ] **Step 6: 运行规则测试**

Run: `php tests/client_diagnostics_policy_test.php`

Expected: 授权过滤、双设备去重和执行器归属断言全部通过。

- [ ] **Step 7: 提交 Task 2**

```bash
git add app/client/validate/ClientDiagnosticsValidate.php app/client/controller/v1/ClientDiagnosticsController.php app/common/server/ClientDiagnosticsServer.php app/client/route/index.php tests/client_diagnostics_policy_test.php
git commit -m "feat: expose client status diagnostics"
```

---

### Task 3: 客户端连接健康快照与本地能力

**Files:**
- Modify: `/Users/wind/code/gf-auto/ws_client.py`
- Modify: `/Users/wind/code/gf-auto/goofish_live.py`
- Modify: `/Users/wind/code/gf-auto/local_api.py`
- Modify: `/Users/wind/code/gf-auto/event_outbox.py`
- Create: `/Users/wind/code/gf-auto/tests/test_runtime_health.py`

**Interfaces:**
- Produces: `GatewayClient.health_snapshot() -> dict`、`XianyuLive.health_snapshot() -> dict`、`EventOutbox.status_summary(now=None) -> dict`。
- Local `/health` 增加 `instanceId`、`imRegistered`、`gatewayBound`，保留现有字段兼容性。

- [ ] **Step 1: 编写失败测试**

```python
def test_gateway_health_requires_completed_bind(self):
    client = GatewayClient.__new__(GatewayClient)
    client.ws = object()
    client.gateway_bound = False
    client.last_pong_at = 0
    self.assertFalse(client.health_snapshot()['bound'])

def test_blocked_outbox_is_error(self):
    summary = outbox.status_summary(now=1_000)
    self.assertEqual(summary['blockedCount'], 1)
```

测试还必须覆盖：bind 成功设置状态、断线 finally 清除状态、pong 更新时间、IM 完成 init 后才为已注册、IM 断线清除、pending 最老年龄。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python3 -m unittest tests.test_runtime_health -v`

Expected: FAIL，提示快照方法或字段不存在。

- [ ] **Step 3: 实现 Gateway 健康状态**

在 `GatewayClient` 中维护：

```python
self.gateway_bound = False
self.bound_at = 0.0
self.last_pong_at = 0.0

def health_snapshot(self):
    return {
        'connected': self.ws is not None,
        'bound': self.gateway_bound,
        'boundAt': self.bound_at,
        'lastPongAt': self.last_pong_at,
        'executorGeneration': self.executor_generation,
    }
```

只有 `client.bind.ack.success=true` 后设置 bound；`server.pong` 更新时间；连接 finally 清除 connected/bound，但保留最后健康时间供诊断展示。

- [ ] **Step 4: 实现闲鱼 IM 健康状态**

在 `XianyuLive` 中维护 `im_registered`、`im_registered_at`、`last_im_message_at`。只有 `await self.init(websocket)` 成功后设置已注册；收到任意协议帧更新时间；连接 finally 清除 `im_registered`。

`XianyuLive.health_snapshot()` 合并登录状态、账号信息、IM、Gateway、端口、实例标识和 Outbox 摘要，不调用登录检查接口。

- [ ] **Step 5: 补充 Outbox 和本地 health**

`EventOutbox.status_summary()` 一次查询返回：

```python
{
    'pendingCount': 0,
    'blockedCount': 0,
    'oldestPendingAt': 0.0,
    'oldestPendingAge': 0.0,
}
```

`GET /health` 返回实际实例标识和两条连接的完成态，便于发现端口被其他实例占用。

- [ ] **Step 6: 运行定向及现有连接测试**

Run: `python3 -m unittest tests.test_runtime_health tests.test_event_outbox tests.test_sync_ack tests.test_token_refresh -v`

Expected: 全部通过。

- [ ] **Step 7: 提交 Task 3**

```bash
git add ws_client.py goofish_live.py local_api.py event_outbox.py tests/test_runtime_health.py
git commit -m "feat: expose client runtime health"
```

---

### Task 4: 客户端状态检测编排

**Files:**
- Create: `/Users/wind/code/gf-auto/status_diagnostics.py`
- Modify: `/Users/wind/code/gf-auto/ws_client.py`
- Create: `/Users/wind/code/gf-auto/tests/test_status_diagnostics.py`
- Modify: `/Users/wind/code/gf-auto/tests/test_gateway_login.py`

**Interfaces:**
- Consumes: 当前 `gateway_auth_manager`、实例列表、`lives`、运行线程及应用路径。
- Produces: `StatusDiagnostics.run() -> {'status', 'counts', 'local', 'stores', 'checkedAt'}` 和 `gateway_client_diagnostics(access_token, instances) -> dict`。

- [ ] **Step 1: 编写失败测试，固定分级和隔离行为**

```python
def test_error_wins_but_unknown_is_counted_separately(self):
    result = summarize([
        item('normal'), item('unknown'), item('error')
    ])
    self.assertEqual(result['status'], 'error')
    self.assertEqual(result['counts']['unknown'], 1)

def test_gateway_failure_keeps_local_results(self):
    result = diagnostics.run()
    self.assertEqual(result['local'][0]['status'], 'normal')
    self.assertEqual(result['gateway']['status'], 'unknown')
```

测试还必须覆盖：blocked Outbox 为异常、pending 超过 10 分钟为异常、商品空列表正常、登录过期使商品能力未知、其他 MTOP 失败显示原始原因、多店铺隔离、无运行实例。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python3 -m unittest tests.test_status_diagnostics -v`

Expected: FAIL，提示 `status_diagnostics` 不存在。

- [ ] **Step 3: 新增 Gateway HTTP helper**

```python
GATEWAY_DIAGNOSTICS_URL = 'https://plugin-gateway.yinghuasuan.com/api/v1/client/diagnostics'

def gateway_client_diagnostics(access_token, instances):
    # timeout=15；code=200 返回 data；HTTP/code 401 抛 GatewayTokenError。
```

测试验证 Bearer Header、请求体、`200` 成功和 `401`。

- [ ] **Step 4: 实现检测器与分级**

```python
class StatusDiagnostics:
    def __init__(self, auth_manager, auth, instances, lives, run_threads, pub_id): ...

    def run(self): ...
```

检测器必须先调用 `auth_manager.refresh(force=True)` 获取当前授权；失败时授权和 Gateway 项为 `unknown`，但继续本机检测。检测项独立捕获异常并输出 `key/title/status/detail/action`。

本机检查包括版本、两个域名 DNS/连通、Chrome、目录和已有文件权限、SQLite 回滚事务、本地端口、Gateway 完成态与 Outbox。

运行实例的商品管理能力直接调用 `live.xianyu.search_seller_items(1, 1)`，不得调用 `_call_seller_api_with_token_retry()` 或 `get_seller_goods()`。

- [ ] **Step 5: 运行检测器与登录测试**

Run: `python3 -m unittest tests.test_status_diagnostics tests.test_gateway_login -v`

Expected: 全部通过。

- [ ] **Step 6: 提交 Task 4**

```bash
git add status_diagnostics.py ws_client.py tests/test_status_diagnostics.py tests/test_gateway_login.py
git commit -m "feat: diagnose xianyu client status"
```

---

### Task 5: 独立状态检测菜单与结果页面

**Files:**
- Modify: `/Users/wind/code/gf-auto/dashboard.py`
- Create: `/Users/wind/code/gf-auto/tests/test_status_diagnostics_dashboard.py`

**Interfaces:**
- Consumes: `StatusDiagnostics.run()` 结果。
- Produces: 导航键 `diagnostics`、`_show_diagnostics()`、`_start_status_diagnostics()`、`_finish_status_diagnostics(result, error)`。

- [ ] **Step 1: 编写失败测试，固定点击才检测**

```python
def test_opening_page_does_not_start_detection(self):
    app._start_status_diagnostics = Mock()
    app._show_diagnostics()
    app._start_status_diagnostics.assert_not_called()

def test_duplicate_click_is_ignored(self):
    app.diagnostics_running = True
    app._start_status_diagnostics()
    thread.assert_not_called()
```

测试还要验证导航顺序为运行日志、状态检测、软件更新、设置，以及 worker 完成后通过 `root.after()` 回主线程更新结果。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python3 -m unittest tests.test_status_diagnostics_dashboard -v`

Expected: FAIL，提示页面方法不存在。

- [ ] **Step 3: 新增菜单和页面状态**

在 `XianyuDesktopApp` 初始化中增加：

```python
self.diagnostics_running = False
self.diagnostics_result = None
```

导航新增 `('diagnostics', '状态检测')`，位置在 `update` 前。打开页面只渲染空状态和“立即检测”按钮。

- [ ] **Step 4: 实现后台检测与结果渲染**

点击后禁用按钮并显示“正在检测”；工作线程调用 `StatusDiagnostics.run()`；完成后展示：

1. 总体状态和四类数量。
2. 本机环境检测项。
3. 每店铺状态和检测项。

每项只显示用户能理解的结果与处理建议，不把异常堆栈或底层协议内容直接展示给用户。结果区域使用 Canvas + Frame 滚动，保证多店铺时内容可访问。

- [ ] **Step 5: 接入明确处理动作**

- `start_instance` 调用现有 `_start_instance(instance)`。
- `relogin` 调用现有 `_force_relogin(instance)`。
- `check_update` 切换到软件更新页并调用现有 `_start_update_check()`。
- 其他问题只打开说明弹窗，不自动修改状态。

- [ ] **Step 6: 运行页面测试和客户端完整测试**

Run: `python3 -m unittest discover -s tests -v`

Expected: 全部测试通过。

- [ ] **Step 7: 静态检查与人工启动**

Run: `python3 -m py_compile dashboard.py status_diagnostics.py ws_client.py goofish_live.py local_api.py event_outbox.py`

Expected: 无输出且退出码为 0。

人工启动后验证：状态检测位于软件更新上方；进入页面不自动检测；点击后界面不卡顿；单店、多店结果不会串店。

- [ ] **Step 8: 提交 Task 5**

```bash
git add dashboard.py tests/test_status_diagnostics_dashboard.py
git commit -m "feat: add status diagnostics page"
```

---

### Task 6: 跨仓最终验证

**Files:**
- Verify only: `/Users/wind/code/gf-auto`
- Verify only: `/Users/wind/wwwroot/yhs-plugin-gateway`
- Verify untouched: `/Users/wind/wwwroot/yhs-bus`

**Interfaces:**
- Consumes: Tasks 1-5 的实现。
- Produces: 可上线的 Gateway 诊断接口和可打包的客户端代码。

- [ ] **Step 1: Gateway 全量语法检查和规则测试**

Run:

```bash
php tests/client_diagnostics_policy_test.php
find app/client app/common/library/ClientDiagnosticsPolicy.php app/common/server/ClientDiagnosticsServer.php -name '*.php' -print0 | xargs -0 -n1 php -l
```

Expected: 断言及语法检查全部通过。

- [ ] **Step 2: 客户端完整测试和编译检查**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile dashboard.py status_diagnostics.py ws_client.py goofish_live.py local_api.py event_outbox.py
```

Expected: 全部测试通过，编译检查无输出。

- [ ] **Step 3: 检查提交范围**

Run: `git status --short`，分别在两个修改仓库执行。

Expected: 只剩用户原有的无关修改；`yhs-bus` 无本功能变更。不得提交 Gateway 后台页面改动、`200`、`300`，也不得提交客户端 `.DS_Store`、`.idea` 和抓包文档。

- [ ] **Step 4: 汇总上线顺序**

先部署 Gateway，再打包客户端。旧客户端 bind 没有有效 presence 时仍可工作，只是状态检测无法精确识别该旧连接；新客户端上线后自动具备完整诊断数据。
