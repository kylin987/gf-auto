from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox

from loguru import logger

from app_paths import (
    chrome_account_from_cookie_file,
    instance_chrome_profile_dir,
    instance_cookie_file,
    instance_log_dir,
    load_instances,
    new_instance,
    save_instances,
)
from app_version import APP_VERSION
import updater


UI_FONT = 'PingFang SC' if os.name == 'posix' else 'Microsoft YaHei'
C = {
    'bg': '#f5f3ef', 'surface': '#fffefd', 'ink': '#242421', 'muted': '#77736c',
    'line': '#e6e1d9', 'nav': '#282825', 'nav_muted': '#ada9a0', 'red': '#b6332d',
    'red_soft': '#f8e8e5', 'green': '#23825f', 'green_soft': '#e7f5ef',
    'gold': '#bd7b12', 'gold_soft': '#fbf1dc', 'gray_soft': '#efede8',
}


class XianyuDesktopApp:
    """多闲鱼店铺实例的桌面工作台。"""

    def __init__(self, root, gateway_auth=None):
        self.root = root
        self.gateway_auth = gateway_auth or {}
        self.instances = load_instances()
        self.selected_id = self.instances[0]['id'] if self.instances else ''
        self.lives = {}
        self.states = {}
        self.events = []
        self.log_queue = queue.Queue()
        self._sink_id = logger.add(self.log_queue.put, level='INFO', enqueue=True, format=self._format_log)
        self._sync_authorized_stores()

        self.root.title(f'影划算店铺插件 v{APP_VERSION}')
        self.root.geometry('1180x760')
        self.root.minsize(980, 650)
        self.root.configure(bg=C['bg'])
        self._build_shell()
        self._show_overview()
        self.root.after(100, self._poll_logs)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _authorized_stores(self):
        scope = self.gateway_auth.get('scope') or {}
        result = []
        for store in scope.get('stores') or []:
            if not isinstance(store, dict) or int(store.get('id') or 0) <= 0:
                continue
            result.append({
                'id': int(store.get('id') or 0),
                'storeName': str(store.get('storeName') or store.get('sellerNick') or '').strip(),
                'sellerNick': str(store.get('sellerNick') or '').strip(),
                'platformShopId': str(store.get('platformShopId') or '').strip(),
            })
        return result

    @staticmethod
    def _format_log(record):
        instance_id = str(record['extra'].get('instance_id') or '')
        return f"{record['time']:HH:mm:ss}|{instance_id}|{record['message']}\n"

    def _sync_authorized_stores(self):
        """迁移旧单店数据时，只用 Cookie 的实际闲鱼 ID 进行精确绑定。"""
        stores = self._authorized_stores()
        stores_by_platform_id = {
            str(store['platformShopId']): store
            for store in stores if store.get('platformShopId')
        }
        changed = False
        for instance in self.instances:
            actual_shop_id = str(chrome_account_from_cookie_file(instance_cookie_file(instance['id'])).get('userId') or '')
            matched_store = stores_by_platform_id.get(actual_shop_id)
            if matched_store:
                if int(instance.get('storeId') or 0) != matched_store['id']:
                    instance.update({
                        'storeId': matched_store['id'], 'storeName': matched_store['storeName'],
                        'name': matched_store['storeName'] or instance.get('name') or '闲鱼店铺',
                        'platformShopId': matched_store['platformShopId'],
                    })
                    changed = True
                continue
            if len(stores) == 1 and int(instance.get('storeId') or 0) == 0:
                store = stores[0]
                if int(instance.get('storeId') or 0) == 0:
                    instance.update({
                        'storeId': store['id'], 'storeName': store['storeName'],
                        'name': store['storeName'] or instance.get('name') or '闲鱼店铺',
                        'platformShopId': store['platformShopId'],
                    })
                    changed = True
        if changed:
            save_instances(self.instances)

    def _build_shell(self):
        self.shell = tk.Frame(self.root, bg=C['bg'])
        self.shell.pack(fill='both', expand=True)
        self.nav = tk.Frame(self.shell, bg=C['nav'], width=202)
        self.nav.pack(side='left', fill='y')
        self.nav.pack_propagate(False)
        brand = tk.Frame(self.nav, bg=C['nav'])
        brand.pack(fill='x', padx=20, pady=(22, 27))
        mark = tk.Canvas(brand, width=29, height=29, bg=C['red'], highlightthickness=0)
        mark.pack(side='left', padx=(0, 9))
        mark.create_text(14, 14, text='影', fill='#fff', font=(UI_FONT, 14, 'bold'))
        tk.Label(brand, text='店铺插件', bg=C['nav'], fg='#fff', font=(UI_FONT, 15, 'bold')).pack(side='left')
        tk.Label(self.nav, text='工作台', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 10, 'bold')).pack(anchor='w', padx=24)
        self.nav_buttons = {}
        for key, label in [('overview', '店铺概览'), ('events', '实时事件'), ('stores', '店铺实例')]:
            button = tk.Button(self.nav, text=label, command=lambda value=key: self._navigate(value),
                               anchor='w', padx=23, pady=10, bd=0, relief='flat', cursor='hand2',
                               bg=C['nav'], fg='#ddd9d0', activebackground=C['red'], activeforeground='#fff',
                               font=(UI_FONT, 11, 'bold'))
            button.pack(fill='x', padx=11, pady=2)
            self.nav_buttons[key] = button
        tk.Label(self.nav, text='客户端', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 10, 'bold')).pack(anchor='w', padx=24, pady=(23, 0))
        tk.Button(self.nav, text='更新与设置', command=self._start_update_check, anchor='w', padx=23, pady=10,
                  bd=0, relief='flat', cursor='hand2', bg=C['nav'], fg='#ddd9d0', activebackground=C['red'],
                  activeforeground='#fff', font=(UI_FONT, 11, 'bold')).pack(fill='x', padx=11, pady=2)
        account = self.gateway_auth.get('user') or {}
        tk.Label(self.nav, text=f"登录子账号\n{account.get('username') or '未知'}\n\nv{APP_VERSION}",
                 justify='left', anchor='w', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 10)).pack(side='bottom', fill='x', padx=23, pady=22)
        self.main = tk.Frame(self.shell, bg=C['bg'])
        self.main.pack(side='left', fill='both', expand=True)

    def _navigate(self, view):
        for button in self.nav_buttons.values():
            button.configure(bg=C['nav'])
        if view in self.nav_buttons:
            self.nav_buttons[view].configure(bg=C['red'])
        if view == 'overview':
            self._show_overview()
        elif view == 'stores':
            self._show_stores()
        else:
            self._show_events()

    def _clear_main(self):
        for widget in self.main.winfo_children():
            widget.destroy()

    def _header(self, title, subtitle=''):
        header = tk.Frame(self.main, bg=C['surface'], height=72)
        header.pack(fill='x')
        header.pack_propagate(False)
        left = tk.Frame(header, bg=C['surface'])
        left.pack(side='left', padx=27, pady=14)
        tk.Label(left, text=title, bg=C['surface'], fg=C['ink'], font=(UI_FONT, 18, 'bold')).pack(anchor='w')
        if subtitle:
            tk.Label(left, text=subtitle, bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w', pady=(3, 0))
        return header

    def _selected(self):
        return next((item for item in self.instances if item.get('id') == self.selected_id), None)

    def _state(self, instance):
        return self.states.get(instance.get('id'), {'status': 'stopped', 'hint': '尚未启动', 'account': {}})

    def _show_overview(self):
        self._clear_main()
        self._navigate_button('overview')
        header = self._header('店铺概览', '当前运行状态与网关事件')
        tk.Button(header, text='检查更新', command=self._start_update_check, bg=C['surface'], fg=C['red'], bd=0,
                  relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold')).pack(side='right', padx=23)
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        instance = self._selected()
        if not instance:
            self._empty_state(body)
            return
        state = self._state(instance)
        top = tk.Frame(body, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=21, pady=18)
        top.pack(fill='x')
        info = tk.Frame(top, bg=C['surface'])
        info.pack(side='left', fill='x', expand=True)
        tk.Label(info, text=instance.get('name') or '未命名店铺', bg=C['surface'], fg=C['ink'], font=(UI_FONT, 20, 'bold')).pack(anchor='w')
        account = state.get('account') or {}
        shop_id = account.get('userId') or instance.get('platformShopId') or '未绑定'
        nick = account.get('nick') or '等待连接 Chrome'
        tk.Label(info, text=f'Chrome 闲鱼：{nick}  ·  ID：{shop_id}', bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w', pady=(7, 0))
        tag_text, tag_bg, tag_fg = self._status_style(state.get('status'))
        tag = tk.Label(top, text=tag_text, bg=tag_bg, fg=tag_fg, padx=10, pady=5, font=(UI_FONT, 10, 'bold'))
        tag.pack(side='right', padx=(8, 11))
        action = '停止运行' if state.get('status') == 'running' else '启动运行'
        tk.Button(top, text=action, command=lambda: self._toggle_instance(instance), bg=C['red'] if action == '启动运行' else C['ink'], fg='#fff',
                  activebackground=C['red'], activeforeground='#fff', bd=0, relief='flat', padx=18, pady=10,
                  cursor='hand2', font=(UI_FONT, 11, 'bold')).pack(side='right')
        stats = tk.Frame(body, bg=C['bg'])
        stats.pack(fill='x', pady=14)
        events = [item for item in self.events if item.get('instanceId') == instance.get('id')]
        for label, value, color in [('接收消息', sum(item['type'] == 'receive' for item in events), C['ink']), ('网关上报', sum(item['type'] == 'report' for item in events), C['green']), ('订单事件', sum(item['type'] == 'order' for item in events), C['gold']), ('待关注', sum(item['type'] == 'error' for item in events), C['red'])]:
            card = tk.Frame(stats, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=15, pady=13)
            card.pack(side='left', fill='x', expand=True, padx=(0, 10))
            tk.Label(card, text=label, bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w')
            tk.Label(card, text=str(value), bg=C['surface'], fg=color, font=(UI_FONT, 23, 'bold')).pack(anchor='w', pady=(4, 0))
        panels = tk.Frame(body, bg=C['bg'])
        panels.pack(fill='both', expand=True)
        logs = tk.Frame(panels, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'])
        logs.pack(side='left', fill='both', expand=True, padx=(0, 10))
        self._panel_header(logs, '实时事件', lambda: self._navigate('events'))
        self.overview_log = self._make_log_widget(logs)
        self._render_log_widget(self.overview_log, instance.get('id'))
        right = tk.Frame(panels, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], width=260)
        right.pack(side='left', fill='y')
        right.pack_propagate(False)
        self._panel_header(right, '店铺实例', lambda: self._navigate('stores'))
        configured_store_ids = {int(store.get('storeId') or 0) for store in self.instances}
        for store in self.instances:
            item = tk.Frame(right, bg=C['surface'])
            item.pack(fill='x', padx=14, pady=9)
            current = store.get('id') == instance.get('id')
            status, bg, fg = self._status_style(self._state(store).get('status'))
            tk.Button(item, text=store.get('name') or '未命名店铺', command=lambda sid=store.get('id'): self._select_instance(sid),
                      anchor='w', bg=C['surface'], fg=C['red'] if current else C['ink'], bd=0, relief='flat',
                      cursor='hand2', font=(UI_FONT, 10, 'bold')).pack(side='left', fill='x', expand=True)
            tk.Label(item, text=status, bg=bg, fg=fg, font=(UI_FONT, 8, 'bold'), padx=5, pady=2).pack(side='right')
        for store in self._authorized_stores():
            if store['id'] in configured_store_ids:
                continue
            item = tk.Frame(right, bg=C['surface'])
            item.pack(fill='x', padx=14, pady=9)
            left = tk.Frame(item, bg=C['surface'])
            left.pack(side='left', fill='x', expand=True)
            tk.Label(left, text=store['storeName'] or f"闲鱼店铺 {store['id']}", bg=C['surface'], fg=C['ink'], font=(UI_FONT, 10, 'bold')).pack(anchor='w')
            tk.Label(left, text='已授权，尚未配置登录态', bg=C['surface'], fg=C['muted'], font=(UI_FONT, 8)).pack(anchor='w', pady=(3, 0))
            tk.Button(item, text='添加', command=lambda value=store: self._add_instance(value), bg=C['red_soft'], fg=C['red'],
                      bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 9, 'bold'), padx=7, pady=4).pack(side='right')

    def _navigate_button(self, key):
        for name, button in self.nav_buttons.items():
            button.configure(bg=C['red'] if name == key else C['nav'])

    def _panel_header(self, parent, title, action):
        row = tk.Frame(parent, bg=C['surface'], height=45)
        row.pack(fill='x')
        row.pack_propagate(False)
        tk.Label(row, text=title, bg=C['surface'], fg=C['ink'], font=(UI_FONT, 11, 'bold')).pack(side='left', padx=15, pady=13)
        tk.Button(row, text='查看全部', command=action, bg=C['surface'], fg=C['red'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 9, 'bold')).pack(side='right', padx=11)

    def _make_log_widget(self, parent):
        frame = tk.Frame(parent, bg=C['surface'])
        frame.pack(fill='both', expand=True)
        text = tk.Text(frame, bg=C['surface'], fg=C['ink'], relief='flat', bd=0, wrap='word', padx=15, pady=8, font=(UI_FONT, 10), state='disabled')
        text.tag_configure('time', foreground=C['muted'])
        text.tag_configure('error', foreground=C['red'])
        text.tag_configure('ok', foreground=C['green'])
        text.tag_configure('order', foreground=C['gold'])
        scroll = tk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        return text

    def _show_events(self):
        self._clear_main()
        self._navigate_button('events')
        header = self._header('实时事件', '仅显示对业务有帮助的消息、任务、订单与异常')
        tk.Button(header, text='清空当前日志', command=self._clear_events, bg=C['surface'], fg=C['red'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold')).pack(side='right', padx=23)
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        self.events_log = self._make_log_widget(body)
        self._render_log_widget(self.events_log, None)

    def _show_stores(self):
        self._clear_main()
        self._navigate_button('stores')
        header = self._header('店铺实例', '每个实例使用独立的 Cookie、Chrome Profile、连接与日志')
        tk.Button(header, text='启动全部', command=self._start_all, bg=C['red'], fg='#fff', bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold'), padx=14, pady=8).pack(side='right', padx=23)
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        configured = {int(item.get('storeId') or 0) for item in self.instances}
        for instance in self.instances:
            self._instance_row(body, instance, configured=True)
        missing = [store for store in self._authorized_stores() if store['id'] not in configured]
        if missing:
            tk.Label(body, text='可添加的后台授权店铺', bg=C['bg'], fg=C['muted'], font=(UI_FONT, 10, 'bold')).pack(anchor='w', pady=(22, 8))
            for store in missing:
                row = tk.Frame(body, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=18, pady=13)
                row.pack(fill='x', pady=4)
                tk.Label(row, text=store['storeName'] or f"闲鱼店铺 {store['id']}", bg=C['surface'], fg=C['ink'], font=(UI_FONT, 12, 'bold')).pack(side='left')
                tk.Label(row, text=f"闲鱼 ID：{store['platformShopId'] or '网关未提供'}", bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(side='left', padx=14)
                tk.Button(row, text='添加实例', command=lambda value=store: self._add_instance(value), bg=C['red_soft'], fg=C['red'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold'), padx=12, pady=6).pack(side='right')
        if not self.instances and not missing:
            self._empty_state(body)

    def _instance_row(self, parent, instance, configured=True):
        row = tk.Frame(parent, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=18, pady=14)
        row.pack(fill='x', pady=4)
        state = self._state(instance)
        tk.Label(row, text=instance.get('name') or '未命名店铺', bg=C['surface'], fg=C['ink'], font=(UI_FONT, 13, 'bold')).grid(row=0, column=0, sticky='w')
        tk.Label(row, text=f"后台店铺 #{instance.get('storeId') or '未绑定'}  ·  闲鱼 ID：{instance.get('platformShopId') or '待网关升级'}", bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).grid(row=1, column=0, sticky='w', pady=(5, 0))
        row.grid_columnconfigure(0, weight=1)
        status, bg, fg = self._status_style(state.get('status'))
        tk.Label(row, text=status, bg=bg, fg=fg, font=(UI_FONT, 9, 'bold'), padx=8, pady=4).grid(row=0, column=1, rowspan=2, padx=8)
        toggle_text = '停止' if state.get('status') == 'running' else '启动'
        tk.Button(row, text=toggle_text, command=lambda item=instance: self._toggle_instance(item), bg=C['red'] if toggle_text == '启动' else C['ink'], fg='#fff', bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold'), padx=13, pady=7).grid(row=0, column=2, rowspan=2, padx=(3, 0))
        tk.Button(row, text='重新登录', command=lambda item=instance: self._force_relogin(item), bg=C['surface'], fg=C['red'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold')).grid(row=0, column=3, rowspan=2, padx=(8, 0))

    def _empty_state(self, parent):
        tk.Label(parent, text='还没有可运行的闲鱼店铺', bg=C['bg'], fg=C['ink'], font=(UI_FONT, 17, 'bold')).pack(pady=(80, 8))
        tk.Label(parent, text='请检查子账号的闲鱼店铺授权，或在“店铺实例”中添加店铺。', bg=C['bg'], fg=C['muted'], font=(UI_FONT, 11)).pack()

    def _add_instance(self, store):
        instance = new_instance(store)
        self.instances.append(instance)
        self.selected_id = instance['id']
        save_instances(self.instances)
        self._event(instance, 'system', f'已添加店铺实例：{instance["name"]}')
        self._show_stores()

    def _select_instance(self, instance_id):
        self.selected_id = instance_id
        self._show_overview()

    def _toggle_instance(self, instance):
        if self._state(instance).get('status') in ('running', 'starting'):
            self._stop_instance(instance)
        else:
            self._start_instance(instance)

    def _start_all(self):
        for instance in self.instances:
            if self._state(instance).get('status') == 'stopped':
                self._start_instance(instance)
        self._show_stores()

    def _start_instance(self, instance):
        if int(instance.get('storeId') or 0) <= 0:
            messagebox.showwarning('无法启动', '该实例尚未绑定后台闲鱼店铺。请重新登录或联系管理员配置店铺授权。', parent=self.root)
            return
        state = self.states.setdefault(instance['id'], {})
        if state.get('status') in ('starting', 'running'):
            return
        state.update({'status': 'starting', 'hint': '正在校验 Chrome 登录店铺', 'account': {}})
        self._event(instance, 'system', '实例正在启动，校验 Chrome 登录态')
        threading.Thread(target=self._run_instance, args=(instance,), daemon=True, name=f"fish-{instance['id']}").start()
        self._refresh_current_view()

    def _run_instance(self, instance):
        from goofish_live import XianyuLive
        port = 18000 + (self.instances.index(instance) if instance in self.instances else 0)
        try:
            with logger.contextualize(instance_id=instance['id']):
                live = XianyuLive(
                    cookie_file=instance_cookie_file(instance['id']),
                    gateway_auth=self.gateway_auth,
                    account_changed_callback=lambda account, item=instance: self._chrome_account_changed(item, account),
                    store_id=instance['storeId'], instance_id=instance['id'], instance_name=instance.get('name') or '',
                    chrome_profile_dir=instance_chrome_profile_dir(instance['id']),
                    local_api_port=port, log_dir=instance_log_dir(instance['id']),
                )
                self.lives[instance['id']] = live
                if not live.ensure_login():
                    raise RuntimeError('未能获取有效的闲鱼登录态')
                account = live.current_chrome_account()
                expected = str(instance.get('platformShopId') or '').strip()
                actual = str(account.get('userId') or '').strip()
                if expected and actual != expected:
                    raise RuntimeError(f'Chrome 登录闲鱼 ID {actual or "未识别"} 与后台店铺 ID {expected} 不一致')
                if not expected:
                    raise RuntimeError('网关未提供后台闲鱼 ID，无法安全确认店铺归属')
                self.states[instance['id']].update({'status': 'running', 'hint': '正在监听消息', 'account': account})
                self._event(instance, 'system', f'店铺校验成功，开始监听：{account.get("nick") or actual}')
                asyncio.run(live.main())
        except Exception as exc:
            self.states.setdefault(instance['id'], {}).update({'status': 'error', 'hint': str(exc)})
            self._event(instance, 'error', str(exc))
        finally:
            if self.states.get(instance['id'], {}).get('status') != 'error':
                self.states.setdefault(instance['id'], {}).update({'status': 'stopped', 'hint': '已停止'})
            self.lives.pop(instance['id'], None)
            self.root.after(0, self._refresh_current_view)

    def _stop_instance(self, instance):
        live = self.lives.get(instance['id'])
        if live is not None:
            live._stop_event.set()
            live.stop_local_api()
        self.states.setdefault(instance['id'], {}).update({'status': 'stopped', 'hint': '已停止'})
        self._event(instance, 'system', '已停止监听')
        self._refresh_current_view()

    def _force_relogin(self, instance):
        if self._state(instance).get('status') in ('running', 'starting'):
            messagebox.showinfo('重新登录', '请先停止该店铺实例，再重新登录。', parent=self.root)
            return
        cookie_path = instance_cookie_file(instance['id'])
        try:
            os.remove(cookie_path)
        except FileNotFoundError:
            pass
        self._event(instance, 'system', '已清除当前店铺 Cookie，下次启动会打开独立 Chrome 登录')
        self._refresh_current_view()

    def _chrome_account_changed(self, instance, account):
        self.states.setdefault(instance['id'], {}).setdefault('account', {}).update(account or {})
        self.root.after(0, self._refresh_current_view)

    def _status_style(self, status):
        if status == 'running':
            return '● 正在监听', C['green_soft'], C['green']
        if status == 'starting':
            return '● 正在启动', C['gold_soft'], C['gold']
        if status == 'error':
            return '● 需要处理', C['red_soft'], C['red']
        return '● 未启动', C['gray_soft'], C['muted']

    def _event(self, instance, kind, text):
        self.events.append({'time': time.strftime('%H:%M:%S'), 'instanceId': instance.get('id'), 'store': instance.get('name') or '', 'type': kind, 'text': str(text)})
        self.events = self.events[-500:]

    def _poll_logs(self):
        try:
            while True:
                line = str(self.log_queue.get_nowait())
                if '|' not in line:
                    continue
                stamp, instance_id, text = line.rstrip('\n').split('|', 2)
                if not self._useful_log(text):
                    continue
                instance = next((item for item in self.instances if item.get('id') == instance_id), None)
                self.events.append({
                    'time': stamp,
                    'instanceId': instance.get('id') if instance else '',
                    'store': instance.get('name') if instance else '客户端',
                    'type': self._event_type(text),
                    'text': text,
                })
                self.events = self.events[-500:]
                self._refresh_logs_only()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_logs)

    @staticmethod
    def _useful_log(text):
        return any(key in text for key in ('收到买家', '上报网关', '网关任务', '发送消息', '订单', '登录', '连接', '失败', '异常', '校验'))

    @staticmethod
    def _event_type(text):
        if '失败' in text or '异常' in text or '不一致' in text:
            return 'error'
        if '订单' in text:
            return 'order'
        if '上报网关' in text:
            return 'report'
        if '收到' in text:
            return 'receive'
        return 'system'

    def _render_log_widget(self, widget, instance_id):
        if not widget.winfo_exists():
            return
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        for event in self.events[-120:]:
            if instance_id and event.get('instanceId') != instance_id:
                continue
            tag = 'error' if event['type'] == 'error' else ('order' if event['type'] == 'order' else ('ok' if event['type'] in ('report', 'receive') else ''))
            widget.insert('end', f"{event['time']}  ", 'time')
            prefix = f"[{event['store']}] " if not instance_id and event.get('store') else ''
            widget.insert('end', prefix + event['text'] + '\n', tag)
        widget.see('end')
        widget.configure(state='disabled')

    def _refresh_logs_only(self):
        if hasattr(self, 'overview_log'):
            instance = self._selected()
            self._render_log_widget(self.overview_log, instance.get('id') if instance else None)
        if hasattr(self, 'events_log'):
            self._render_log_widget(self.events_log, None)

    def _refresh_current_view(self):
        # 视图重建不会影响后台实例，只同步状态文本和统计数。
        if hasattr(self, 'overview_log') and self.overview_log.winfo_exists():
            self._show_overview()
        elif hasattr(self, 'events_log') and self.events_log.winfo_exists():
            self._show_events()

    def _clear_events(self):
        self.events = []
        self._refresh_logs_only()

    def _start_update_check(self):
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            info = updater.check_for_update(APP_VERSION)
            self.root.after(0, lambda: self._finish_update_check(info, None))
        except Exception as exc:
            self.root.after(0, lambda: self._finish_update_check(None, exc))

    def _finish_update_check(self, info, error):
        if error:
            messagebox.showwarning('检查更新', f'检查更新失败：{error}', parent=self.root)
            return
        if info is None:
            messagebox.showinfo('检查更新', '当前已是最新版本', parent=self.root)
            return
        if messagebox.askyesno('发现新版本', f'发现 v{info.version}，现在下载并安装吗？', parent=self.root):
            threading.Thread(target=self._download_update_worker, args=(info,), daemon=True).start()

    def _download_update_worker(self, info):
        try:
            installer = updater.download_installer(info)
            self.root.after(0, lambda: self._finish_update_download(installer, None))
        except Exception as exc:
            self.root.after(0, lambda: self._finish_update_download(None, exc))

    def _finish_update_download(self, installer, error):
        if error:
            messagebox.showwarning('下载更新', f'下载更新失败：{error}', parent=self.root)
            return
        if messagebox.askyesno('安装更新', '更新包已下载完成，是否现在安装？', parent=self.root):
            updater.launch_installer(installer)
            self._on_close()

    def _on_close(self):
        for instance in list(self.instances):
            self._stop_instance(instance)
        try:
            logger.remove(self._sink_id)
        except ValueError:
            pass
        self.root.destroy()
