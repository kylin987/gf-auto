import asyncio
import os
import queue
import sys
import threading
import time
import tkinter as tk

from loguru import logger

from ws_client import GatewayLoginError, gateway_login, load_login_config


def _check_chrome():
    """延迟导入 Chrome 检测，加快启动到登录页的速度。"""
    try:
        from cookie_auth import check_chrome_installed
        return check_chrome_installed()
    except ImportError:
        from cookie_auth import find_chrome_binary
        path = os.environ.get('XY_CHROME_PATH') or find_chrome_binary()
        if path:
            return True, path, ''
        return False, None, ('未检测到 Google Chrome，请先安装浏览器后再运行。'
                             '下载地址: https://www.google.cn/chrome/')


# 内置监听配置，不再在界面上编辑
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8000
APP_VERSION = '0.1.3'


FRIENDLY_RULES = [
    ('使用本地 cookie 登录成功', '已读取本地登录信息'),
    ('需要登录闲鱼卖家中心', '请在弹出的 Chrome 窗口完成登录'),
    ('已获取登录 cookie', '登录信息已获取'),
    ('重新登录成功', '登录状态已刷新'),
    ('登录态已失效', '登录状态已过期，正在重新登录'),
    ('登录成功', '登录成功'),
    ('登录失败，已停止', '登录失败，已停止'),
    ('登录失败', '登录失败，请重试'),
    ('获取token失败', '登录状态异常，请稍后重试'),
    ('本地接口已启动', '本地接口已启动'),
    ('WebSocket 已连接', '服务器连接成功'),
    ('聊天日志已写入', '聊天记录已保存'),
    ('发送给我的信息', '收到新消息'),
    ('user:', '收到新消息'),
    ('运行已启动', '运行已启动'),
    ('运行已停止', '运行已停止'),
    ('客户端就绪', '客户端就绪'),
    ('未检测到 Google Chrome', '未检测到 Chrome，请先安装浏览器'),
    ('启动失败', '启动失败，请检查后重试'),
    ('服务已停止', '服务已停止'),
    ('程序异常退出', '程序遇到问题，已停止'),
]

SUPPRESS_KEYWORDS = [
    'WS 服务端消息', 'sync ack', '收到新结构', 'Traceback', 'File "',
    '.py', 'wss://', 'http', 'cookie', 'token', 'accessToken',
    'refresh_token', 'RGV587', 'body=', 'lwp=', 'code=', '{', '}',
    'INFO', 'ERROR', 'WARNING', 'DEBUG',
]

BUSINESS_LOG_KEYWORDS = [
    '收到买家',
    '收到新消息',
    '上报网关',
    '网关绑定成功',
    '网关错误',
    '收到网关任务',
    '网关任务执行成功',
    '插件任务执行失败',
    '发送消息给买家',
    '查询订单详情完成',
    '订单改价完成',
    '订单消息',
]


def friendly_line(message):
    """把技术日志翻译成小白提示，无法翻译的直接隐藏。"""
    text = str(message).strip()
    for keyword in BUSINESS_LOG_KEYWORDS:
        if keyword in text:
            return text
    for key, friendly in FRIENDLY_RULES:
        if key in text:
            return friendly
    lowered = text.lower()
    for keyword in SUPPRESS_KEYWORDS:
        if keyword.lower() in lowered:
            return None
    return None

if sys.platform == 'darwin':
    UI_FONT = 'PingFang SC'
else:
    UI_FONT = 'Microsoft YaHei'

C = {
    'bg': '#f6f4ef',
    'surface': '#fffdfa',
    'ink': '#3a3833',
    'muted': '#7d7a72',
    'line': '#e8e4dc',
    'titlebar': '#2b2b2b',
    'titlebar_text': '#f2f0ea',
    'yellow': '#ffd84d',
    'yellow_strong': '#f6c94a',
    'yellow_dark': '#e6b93f',
    'yellow_soft': '#fdf6dc',
    'green': '#3d9e6f',
    'green_soft': '#e3f4ec',
    'gray': '#9a968e',
    'gray_soft': '#f1efe9',
    'danger': '#b04a3e',
}


class LoginView:
    def __init__(self, root, on_success):
        self.root = root
        self.on_success = on_success
        self.root.title('闲鱼 AI 客服助手 - 登录')
        self.root.geometry('880x640')
        self.root.minsize(720, 520)
        self.root.configure(bg=C['bg'])
        self._build()

    def _build(self):
        frame = tk.Frame(self.root, bg=C['bg'])
        frame.pack(fill='both', expand=True)

        top = tk.Frame(frame, bg=C['bg'])
        top.pack(pady=(64, 0))
        icon = tk.Canvas(top, width=52, height=52, bg=C['yellow'],
                         highlightthickness=0)
        icon.pack()
        icon.create_text(26, 26, text='影', fill=C['ink'],
                         font=(UI_FONT, 26, 'bold'))
        tk.Label(top, text='闲鱼 AI 客服助手', bg=C['bg'], fg=C['ink'],
                 font=(UI_FONT, 22, 'bold')).pack(pady=(14, 6))
        tk.Label(top, text='需使用影划算后台-系统-子账号管理，创建的子账号进行登录',
                 bg=C['bg'], fg=C['muted'], font=(UI_FONT, 11),
                 wraplength=420, justify='center').pack()

        card = tk.Frame(frame, bg=C['surface'], highlightthickness=1,
                        highlightbackground=C['line'], padx=36, pady=30,
                        width=420)
        card.pack(pady=28)

        tk.Label(card, text='账号', bg=C['surface'], fg=C['ink'],
                 font=(UI_FONT, 11, 'bold')).pack(anchor='w')
        config = load_login_config()
        self.username_var = tk.StringVar(value=config.get('username', ''))
        self.username_entry = self._make_entry(card, self.username_var)
        self.username_entry.pack(fill='x', pady=(6, 18))

        tk.Label(card, text='密码', bg=C['surface'], fg=C['ink'],
                 font=(UI_FONT, 11, 'bold')).pack(anchor='w')
        pwd_frame = tk.Frame(card, bg=C['surface'])
        pwd_frame.pack(fill='x', pady=(6, 22))
        self.password_var = tk.StringVar(value=config.get('password', ''))
        self.password_entry_wrap = self._make_entry(pwd_frame, self.password_var, show='*')
        self.password_entry_wrap.pack(fill='x', expand=True)
        self.password_entry = self.password_entry_wrap.entry

        self.login_btn = tk.Button(
            card, text='登录', command=self._login,
            bg=C['yellow'], fg=C['ink'], activebackground=C['yellow_strong'],
            activeforeground=C['ink'], relief='flat', bd=0,
            font=(UI_FONT, 13, 'bold'), pady=12, cursor='hand2',
        )
        self.login_btn.pack(fill='x')

        self.error_var = tk.StringVar()
        tk.Label(card, textvariable=self.error_var, bg=C['surface'],
                 fg=C['danger'], font=(UI_FONT, 10), wraplength=320,
                 justify='left').pack(pady=(14, 0), anchor='w')

        self.root.bind('<Return>', lambda _e: self._login())

    def _make_entry(self, parent, var, show=None):
        wrap = tk.Frame(parent, bg='#fbfaf6', highlightthickness=1,
                        highlightbackground=C['line'],
                        highlightcolor=C['yellow_dark'])
        entry = tk.Entry(wrap, textvariable=var, show=show, font=(UI_FONT, 12),
                         bg='#fbfaf6', fg=C['ink'], relief='flat', bd=0,
                         insertbackground=C['ink'])
        entry.pack(fill='x', padx=12, pady=9)
        entry.bind('<FocusIn>', lambda _e: wrap.configure(
            highlightbackground=C['yellow_dark'], highlightcolor=C['yellow_dark']))
        entry.bind('<FocusOut>', lambda _e: wrap.configure(
            highlightbackground=C['line']))
        wrap.entry = entry
        return wrap

    def _login(self):
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username and not password:
            self.error_var.set('请输入账号和密码')
            return
        if not username:
            self.error_var.set('请输入账号')
            return
        if not password:
            self.error_var.set('请输入密码')
            return
        self.error_var.set('')
        self.login_btn.configure(state='disabled')
        self.login_btn.configure(text='正在登录...', bg=C['yellow_soft'])
        threading.Thread(
            target=self._do_login, args=(username, password), daemon=True,
        ).start()

    def _do_login(self, username, password):
        try:
            data = gateway_login(username, password)
        except GatewayLoginError as exc:
            self.root.after(0, lambda: self._login_failed(f'登录失败：{exc}'))
            return
        except Exception:
            self.root.after(0, lambda: self._login_failed('登录失败：网络异常，请稍后重试'))
            return
        self.root.after(0, lambda: self._login_ok(data))

    def _login_failed(self, message):
        self.login_btn.configure(state='normal', text='登录', bg=C['yellow'])
        self.error_var.set(message)

    def _login_ok(self, data):
        for widget in self.root.winfo_children():
            widget.destroy()
        self.on_success(data)


class XianyuDesktopApp:
    def __init__(self, root, gateway_auth=None):
        self.root = root
        self.gateway_auth = gateway_auth or {}
        self.log_queue = queue.Queue()
        self.worker = None
        self.live = None
        self.running = False
        self.timer_seconds = 0
        self.start_count = 0
        self._timer_after = None

        logger.add(
            self.log_queue.put,
            level='INFO',
            enqueue=True,
            format='{time:HH:mm:ss}|{message}',
        )

        self._build_ui()
        self.root.after(100, self._poll_logs)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self._log(f'登录成功：{self._account_name()}')
        self._log(f'客户端就绪 v{APP_VERSION}，等待启动')
        self._log(self._store_summary())
        self._update_ui()

    def _build_ui(self):
        self.root.title(f'闲鱼 AI 客服助手 v{APP_VERSION}')
        self.root.geometry('880x640')
        self.root.minsize(720, 520)
        self.root.configure(bg=C['bg'])

        self._build_titlebar()

        body = tk.Frame(self.root, bg=C['bg'])
        body.pack(fill='both', expand=True)

        main = tk.Frame(body, bg=C['bg'])
        main.pack(fill='both', expand=True, padx=34, pady=(26, 30))

        # 面板标题 + 状态胶囊
        head = tk.Frame(main, bg=C['bg'])
        head.pack(fill='x')

        head_left = tk.Frame(head, bg=C['bg'])
        head_left.pack(side='left')
        tk.Label(head_left, text='首页', bg=C['bg'], fg=C['muted'],
                 font=(UI_FONT, 11, 'bold')).pack(anchor='w')
        tk.Label(head_left, text='运行状态', bg=C['bg'], fg=C['ink'],
                 font=(UI_FONT, 22, 'bold')).pack(anchor='w')

        self.pill = tk.Frame(head, bg=C['gray_soft'], padx=12, pady=5)
        self.pill.pack(side='right')
        self.pill_dot = tk.Canvas(self.pill, width=9, height=9, bg=C['gray_soft'],
                                  highlightthickness=0)
        self.pill_dot.pack(side='left', padx=(0, 7))
        self.pill_dot.create_oval(1, 1, 8, 8, fill=C['gray'], outline='')
        self.pill_text = tk.Label(self.pill, text='未启动', bg=C['gray_soft'],
                                  fg=C['gray'], font=(UI_FONT, 10, 'bold'))
        self.pill_text.pack(side='left')

        # 运行区域
        run_zone = tk.Frame(main, bg=C['surface'], highlightthickness=1,
                            highlightbackground=C['line'], padx=28, pady=24)
        run_zone.pack(fill='x', pady=(22, 16))

        run_left = tk.Frame(run_zone, bg=C['surface'])
        run_left.pack(side='left', fill='x', expand=True)
        self.status_text = tk.Label(run_left, text='未启动', bg=C['surface'],
                                    fg=C['ink'], font=(UI_FONT, 30, 'bold'))
        self.status_text.pack(anchor='w')
        self.status_hint = tk.Label(run_left, text='等待启动', bg=C['surface'],
                                    fg=C['muted'], font=(UI_FONT, 12))
        self.status_hint.pack(anchor='w', pady=(6, 0))
        self.account_text = tk.Label(
            run_left,
            text=f'子账号：{self._account_name()}',
            bg=C['surface'],
            fg=C['muted'],
            font=(UI_FONT, 11),
        )
        self.account_text.pack(anchor='w', pady=(10, 0))
        store_fg = C['danger'] if not self._store_ids() else C['green']
        self.store_text = tk.Label(
            run_left,
            text=self._store_summary(),
            bg=C['surface'],
            fg=store_fg,
            font=(UI_FONT, 11, 'bold'),
            wraplength=460,
            justify='left',
        )
        self.store_text.pack(anchor='w', pady=(4, 0))

        self.toggle_btn = tk.Button(
            run_zone, text='启动运行', command=self._toggle,
            bg=C['yellow'], fg=C['ink'], activebackground=C['yellow_strong'],
            activeforeground=C['ink'], relief='flat', bd=0,
            highlightthickness=1, highlightbackground=C['yellow_dark'],
            font=(UI_FONT, 14, 'bold'), padx=34, pady=14, cursor='hand2',
        )
        self.toggle_btn.pack(side='right')

        # 统计卡片
        stats = tk.Frame(main, bg=C['bg'])
        stats.pack(fill='x', pady=(0, 18))

        self.timer_value = self._build_stat(stats, '运行时长', '00:00:00')
        self.count_value = self._build_stat(stats, '今日启动', '0 次')

        # 日志面板
        log_panel = tk.Frame(main, bg=C['surface'], highlightthickness=1,
                             highlightbackground=C['line'])
        log_panel.pack(fill='both', expand=True)

        log_head = tk.Frame(log_panel, bg=C['surface'], height=46)
        log_head.pack(fill='x')
        log_head.pack_propagate(False)
        tk.Label(log_head, text='业务日志', bg=C['surface'], fg=C['ink'],
                 font=(UI_FONT, 12, 'bold')).pack(side='left', padx=16)
        tk.Button(log_head, text='清空', command=self._clear_log,
                  bg=C['surface'], fg=C['muted'], activebackground=C['yellow_soft'],
                  activeforeground=C['ink'], relief='flat', bd=0,
                  font=(UI_FONT, 10, 'bold'), cursor='hand2').pack(side='right', padx=12)

        log_body = tk.Frame(log_panel, bg=C['surface'])
        log_body.pack(fill='both', expand=True)
        self.log_text = tk.Text(
            log_body, bg=C['surface'], fg=C['ink'], relief='flat', bd=0,
            font=(UI_FONT, 10), wrap='none', padx=16, pady=10,
            highlightthickness=0,
        )
        self.log_text.tag_configure('time', foreground=C['muted'])
        self.log_text.tag_configure('error', foreground=C['danger'])
        self.log_text.tag_configure('send', foreground=C['green'])
        self.log_text.tag_configure('receive', foreground='#2f6fb2')
        self.log_text.tag_configure('order', foreground='#8a5a00')
        scrollbar = tk.Scrollbar(log_body, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.log_text.configure(state='disabled')

    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg=C['titlebar'], height=46)
        bar.pack(fill='x')
        bar.pack_propagate(False)

        brand = tk.Frame(bar, bg=C['titlebar'])
        brand.pack(side='left', padx=(16, 0))
        icon = tk.Canvas(brand, width=28, height=28, bg=C['yellow'],
                         highlightthickness=0)
        icon.pack(side='left', padx=(0, 9))
        icon.create_text(14, 14, text='影', fill=C['ink'], font=(UI_FONT, 14, 'bold'))
        tk.Label(brand, text='闲鱼 AI 客服助手', bg=C['titlebar'],
                 fg=C['titlebar_text'], font=(UI_FONT, 12, 'bold')).pack(side='left')

    def _build_stat(self, parent, label, value):
        card = tk.Frame(parent, bg=C['surface'], highlightthickness=1,
                        highlightbackground=C['line'], padx=18, pady=14)
        card.pack(side='left', fill='x', expand=True)
        card.pack_propagate(False)
        card.configure(height=84)
        tk.Label(card, text=label, bg=C['surface'], fg=C['muted'],
                 font=(UI_FONT, 10, 'bold')).pack(anchor='w')
        value_label = tk.Label(card, text=value, bg=C['surface'], fg=C['ink'],
                               font=(UI_FONT, 18, 'bold'))
        value_label.pack(anchor='w', pady=(4, 0))
        return value_label

    def _account_name(self):
        user = self.gateway_auth.get('user') or {}
        scope = self.gateway_auth.get('scope') or {}
        return str(user.get('username') or scope.get('username') or '未知子账号')

    def _store_ids(self):
        scope = self.gateway_auth.get('scope') or {}
        ids = scope.get('storeIds') or []
        return [int(item) for item in ids if str(item).isdigit() and int(item) > 0]

    def _store_summary(self):
        scope = self.gateway_auth.get('scope') or {}
        stores = scope.get('stores') or []
        names = []
        for store in stores:
            name = str(store.get('storeName') or store.get('sellerNick') or '').strip()
            sid = store.get('id')
            if name:
                names.append(f'{name}({sid})')
            elif sid:
                names.append(f'店铺 {sid}')
        if names:
            return '可用闲鱼店铺：' + '，'.join(names)
        ids = self._store_ids()
        if ids:
            return '可用闲鱼店铺：' + '，'.join(str(item) for item in ids)
        return '未绑定可用闲鱼店铺，请检查子账号所属商户和闲鱼店铺状态'

    def _log(self, message):
        self.log_queue.put(f'{time.strftime("%H:%M:%S")}|{message}')

    def _poll_logs(self):
        try:
            while True:
                line = str(self.log_queue.get_nowait())
                if '|' in line:
                    log_time, log_msg = line.split('|', 1)
                else:
                    log_time, log_msg = '', line
                friendly = friendly_line(log_msg)
                if friendly is None:
                    continue
                tag = self._log_tag(friendly)
                self.log_text.configure(state='normal')
                self.log_text.insert('end', f'{log_time}  ', ('time',))
                self.log_text.insert('end', f'{friendly}\n', (tag,))
                self.log_text.see('end')
                self.log_text.configure(state='disabled')
        except queue.Empty:
            pass
        self.root.after(100, self._poll_logs)

    def _log_tag(self, message):
        if '失败' in message or '错误' in message or '异常' in message:
            return 'error'
        if '发送' in message or '改价完成' in message:
            return 'send'
        if '收到' in message or '上报网关' in message:
            return 'receive'
        if '订单' in message:
            return 'order'
        return 'normal'

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def _toggle(self):
        if self.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if self.running:
            return
        os.environ['XY_API_HOST'] = DEFAULT_HOST
        os.environ['XY_API_PORT'] = str(DEFAULT_PORT)
        self.running = True
        self.timer_seconds = 0
        self.start_count += 1
        self.count_value.configure(text=f'{self.start_count} 次')
        self.worker = threading.Thread(target=self._run_backend, daemon=True,
                                       name='xianyu-backend')
        self.worker.start()
        self._update_ui()
        self._log(f'运行已启动（{DEFAULT_HOST}:{DEFAULT_PORT}）')
        self._tick_timer()

    def _run_backend(self):
        from goofish_live import XianyuLive
        try:
            if not os.environ.get('XY_SKIP_CHROME_CHECK'):
                installed, _, hint = _check_chrome()
                if not installed:
                    logger.error(hint)
                    self._mark_stopped('未检测到 Chrome，启动失败')
                    return
            live = XianyuLive(
                cookies_str=os.environ.get('XY_COOKIE_STR'),
                cookie_file=os.environ.get('XY_COOKIE_FILE', 'cookies.json'),
                gateway_auth=self.gateway_auth,
            )
            self.live = live
            if not live.ensure_login():
                logger.error('登录失败，程序退出')
                self._mark_stopped('登录失败，已停止')
                return
            logger.info('登录成功，开始运行...')
            asyncio.run(live.main())
        except Exception:
            logger.exception('程序异常退出')
        finally:
            self._mark_stopped('服务已停止')

    def _stop(self):
        if self.live is not None:
            self.live._stop_event.set()
        self.running = False
        if self._timer_after is not None:
            self.root.after_cancel(self._timer_after)
            self._timer_after = None
        self._log('运行已停止')
        self._update_ui()

    def _tick_timer(self):
        if not self.running:
            return
        self.timer_seconds += 1
        hours = self.timer_seconds // 3600
        minutes = (self.timer_seconds % 3600) // 60
        seconds = self.timer_seconds % 60
        self.timer_value.configure(text=f'{hours:02d}:{minutes:02d}:{seconds:02d}')
        self._timer_after = self.root.after(1000, self._tick_timer)

    def _mark_stopped(self, status_text):
        def update():
            if not self.running:
                return
            self.running = False
            if self._timer_after is not None:
                self.root.after_cancel(self._timer_after)
                self._timer_after = None
            self.status_text.configure(text='未启动')
            self.status_hint.configure(text=status_text)
            self._update_ui()

        self.root.after(0, update)

    def _update_ui(self):
        if self.running:
            self.status_text.configure(text='已启动')
            self.status_hint.configure(text='正在运行')
            self.pill.configure(bg=C['green_soft'])
            self.pill_text.configure(text='已启动', bg=C['green_soft'], fg=C['green'])
            self.pill_dot.configure(bg=C['green_soft'])
            self.pill_dot.delete('all')
            self.pill_dot.create_oval(1, 1, 8, 8, fill=C['green'], outline='')
            self.toggle_btn.configure(text='停止运行', bg=C['ink'], fg=C['yellow'],
                                      activebackground='#4a4a4a',
                                      highlightbackground=C['ink'])
        else:
            self.status_text.configure(text='未启动')
            self.status_hint.configure(text='等待启动')
            self.pill.configure(bg=C['gray_soft'])
            self.pill_text.configure(text='未启动', bg=C['gray_soft'], fg=C['gray'])
            self.pill_dot.configure(bg=C['gray_soft'])
            self.pill_dot.delete('all')
            self.pill_dot.create_oval(1, 1, 8, 8, fill=C['gray'], outline='')
            self.toggle_btn.configure(text='启动运行', bg=C['yellow'], fg=C['ink'],
                                      activebackground=C['yellow_strong'],
                                      highlightbackground=C['yellow_dark'])

    def _on_close(self):
        if self.running:
            self._stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    LoginView(root, on_success=lambda auth: XianyuDesktopApp(root, gateway_auth=auth))
    root.mainloop()


if __name__ == '__main__':
    main()
