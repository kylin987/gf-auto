from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox
from pathlib import Path

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
from status_diagnostics import StatusDiagnostics
import updater


UI_FONT = 'PingFang SC' if os.name == 'posix' else 'Microsoft YaHei'
C = {
    'bg': '#fff8e8', 'surface': '#fffef9', 'ink': '#2b2116', 'muted': '#776d5e',
    'line': '#eee2c9', 'nav': '#ffd400', 'nav_active': '#a91f1b', 'nav_muted': '#7a421a',
    'red': '#c7372f', 'red_soft': '#ffe9e5', 'green': '#20815f', 'green_soft': '#e6f6ee',
    'yellow': '#bc2923', 'yellow_deep': '#8f1c19', 'yellow_soft': '#fbe4e1', 'gray_soft': '#f5eddd',
}

WORKBENCH_NAV_ITEMS = (
    ('overview', '运行概览'),
    ('stores', '店铺管理'),
    ('events', '运行日志'),
)
CLIENT_NAV_ITEMS = (
    ('diagnostics', '状态检测'),
    ('update', '软件更新'),
    ('settings', '设置'),
)
DIAGNOSTIC_ACTION_LABELS = {
    'start_instance': '启动实例',
    'relogin': '重新登录',
    'check_update': '检查更新',
    'view_advice': '查看建议',
}


class DownloadProgressDialog:
    """下载更新进度弹窗。"""

    def __init__(self, parent, version: str, total_bytes: int = 0):
        self.top = tk.Toplevel(parent)
        self.top.title(f'正在下载更新 v{version}')
        self.top.geometry('460x220')
        self.top.minsize(440, 200)
        self.top.configure(bg=C['bg'])
        self.top.transient(parent)
        self.top.grab_set()

        # 居中显示
        self.top.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        w = 460
        h = 220
        x = max(0, px + (pw - w) // 2)
        y = max(0, py + (ph - h) // 2)
        self.top.geometry(f'{w}x{h}+{x}+{y}')

        card = tk.Frame(self.top, bg=C['surface'], highlightthickness=1,
                        highlightbackground=C['line'], padx=24, pady=20)
        card.pack(fill='both', expand=True, padx=16, pady=16)

        self.title_label = tk.Label(
            card, text=f'正在下载新版本 v{version}...',
            bg=C['surface'], fg=C['ink'], font=(UI_FONT, 13, 'bold')
        )
        self.title_label.pack(anchor='w')

        self.bar_height = 16
        self.canvas = tk.Canvas(
            card, height=self.bar_height, bg=C['gray_soft'],
            highlightthickness=1, highlightbackground=C['line']
        )
        self.canvas.pack(fill='x', pady=(16, 8))
        self.fill_rect = self.canvas.create_rectangle(
            0, 0, 0, self.bar_height, fill=C['green'], width=0
        )

        info_row = tk.Frame(card, bg=C['surface'])
        info_row.pack(fill='x')

        self.size_label = tk.Label(
            info_row, text='正在连接更新服务器...', bg=C['surface'], fg=C['muted'],
            font=(UI_FONT, 10)
        )
        self.size_label.pack(side='left')

        self.percent_label = tk.Label(
            info_row, text='0.0%', bg=C['surface'], fg=C['ink'],
            font=(UI_FONT, 11, 'bold')
        )
        self.percent_label.pack(side='right')

        self.status_hint = tk.Label(
            card, text='下载完成后将自动校验完整性并提示安装',
            bg=C['surface'], fg=C['muted'], font=(UI_FONT, 9)
        )
        self.status_hint.pack(anchor='w', pady=(10, 0))

    def update_progress(self, downloaded: int, total: int):
        if not self.top.winfo_exists():
            return
        canvas_width = max(self.canvas.winfo_width(), 360)
        if total > 0:
            pct = min(100.0, (downloaded / total) * 100.0)
            down_mb = downloaded / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            fill_width = int((pct / 100.0) * canvas_width)
            self.canvas.coords(self.fill_rect, 0, 0, max(fill_width, 0), self.bar_height)
            self.percent_label.configure(text=f'{pct:.1f}%')
            self.size_label.configure(text=f'{down_mb:.1f} MB / {total_mb:.1f} MB')
        else:
            down_mb = downloaded / (1024 * 1024)
            self.percent_label.configure(text='下载中...')
            self.size_label.configure(text=f'已下载 {down_mb:.1f} MB')

    def set_verifying(self):
        if not self.top.winfo_exists():
            return
        canvas_width = max(self.canvas.winfo_width(), 360)
        self.canvas.coords(self.fill_rect, 0, 0, canvas_width, self.bar_height)
        self.percent_label.configure(text='100.0%')
        self.title_label.configure(text='下载完成，正在校验完整性...')
        self.status_hint.configure(text='正在验证 SHA256 签名，请稍候...')

    def close(self):
        try:
            self.top.grab_release()
            self.top.destroy()
        except Exception:
            pass


class XianyuDesktopApp:
    """多闲鱼店铺实例的桌面工作台。"""

    def __init__(self, root, gateway_auth=None):
        from ws_client import GatewayAuthManager

        self.root = root
        self.gateway_auth = gateway_auth or {}
        self.gateway_auth_manager = GatewayAuthManager(self.gateway_auth)
        scope = self.gateway_auth.get('scope') or {}
        user = self.gateway_auth.get('user') or {}
        self.pub_id = int(scope.get('pubId') or user.get('pubId') or 0)
        authorized_stores = self._authorized_stores()
        self.instances = load_instances(self.pub_id, authorized_stores)
        self.selected_id = self.instances[0]['id'] if self.instances else ''
        self.lives = {}
        self.run_threads = {}
        self.run_generations = {}
        self.states = {}
        self.events = []
        self.current_view = 'overview'
        self.log_queue = queue.Queue()
        self._sink_id = logger.add(self.log_queue.put, level='INFO', enqueue=True, format=self._format_log)
        self._sync_authorized_stores(authorized_stores)

        self.downloading_update = False
        self.download_dialog = None
        self.update_progress_info = None
        self.diagnostics_running = False
        self.diagnostics_result = None
        self.diagnostics_error = ''

        self.app_icon = tk.PhotoImage(file=self._asset_path('assets/brand/fish-app-icon.png'))
        self.brand_logo = self.app_icon.subsample(14, 14)
        self.root.iconphoto(True, self.app_icon)
        self.root.title(f'闲鱼店铺插件 v{APP_VERSION}')
        self.root.geometry('1180x760')
        self.root.minsize(980, 650)
        self.root.configure(bg=C['bg'])
        self._build_shell()
        self._show_overview()
        self.root.after(100, self._poll_logs)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    @staticmethod
    def _asset_path(relative_path):
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        return str(base / relative_path)

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

    def _sync_authorized_stores(self, stores=None):
        """迁移旧单店数据时，只用 Cookie 的实际闲鱼 ID 进行精确绑定。"""
        stores = self._authorized_stores() if stores is None else stores
        stores_by_platform_id = {
            str(store['platformShopId']): store
            for store in stores if store.get('platformShopId')
        }
        changed = False
        for instance in self.instances:
            actual_shop_id = str(chrome_account_from_cookie_file(instance_cookie_file(instance['id'], self.pub_id)).get('userId') or '')
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
            stored_id = int(instance.get('storeId') or 0)
            stored = next((store for store in stores if store['id'] == stored_id), None)
            if stored:
                refreshed = {
                    'storeName': stored['storeName'],
                    'name': stored['storeName'] or instance.get('name') or '闲鱼店铺',
                    'platformShopId': stored['platformShopId'],
                }
                if any(instance.get(key) != value for key, value in refreshed.items()):
                    instance.update(refreshed)
                    changed = True
        configured_ids = {int(instance.get('storeId') or 0) for instance in self.instances}
        for store in stores:
            if store['id'] not in configured_ids:
                self.instances.append(new_instance(store))
                changed = True
        if changed:
            save_instances(self.instances, self.pub_id)

    def _build_shell(self):
        self.shell = tk.Frame(self.root, bg=C['bg'])
        self.shell.pack(fill='both', expand=True)
        self.nav = tk.Frame(self.shell, bg=C['nav'], width=224)
        self.nav.pack(side='left', fill='y')
        self.nav.pack_propagate(False)
        brand = tk.Frame(self.nav, bg=C['nav'])
        brand.pack(fill='x', padx=16, pady=(20, 24))
        tk.Label(brand, image=self.brand_logo, bg=C['nav']).pack(side='left', padx=(0, 8))
        brand_text = tk.Frame(brand, bg=C['nav'])
        brand_text.pack(side='left', fill='x', expand=True)
        tk.Label(brand_text, text='闲鱼店铺插件', bg=C['nav'], fg=C['ink'], font=(UI_FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(brand_text, text='影划算票务', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 9, 'bold')).pack(anchor='w', pady=(2, 0))
        tk.Label(self.nav, text='工作台', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 10, 'bold')).pack(anchor='w', padx=20)
        self.nav_buttons = {}
        for key, label in WORKBENCH_NAV_ITEMS:
            button = tk.Button(self.nav, text=label, command=lambda value=key: self._navigate(value),
                               anchor='w', padx=20, pady=10, bd=0, relief='flat', cursor='hand2',
                               bg=C['nav'], fg=C['ink'], activebackground=C['nav_active'], activeforeground='#fff',
                               font=(UI_FONT, 11, 'bold'))
            button.pack(fill='x', padx=10, pady=2)
            self.nav_buttons[key] = button
        tk.Label(self.nav, text='客户端', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 10, 'bold')).pack(anchor='w', padx=20, pady=(20, 0))
        for key, label in CLIENT_NAV_ITEMS:
            button = tk.Button(self.nav, text=label, command=lambda value=key: self._navigate(value),
                               anchor='w', padx=20, pady=10, bd=0, relief='flat', cursor='hand2',
                               bg=C['nav'], fg=C['ink'], activebackground=C['nav_active'], activeforeground='#fff',
                               font=(UI_FONT, 11, 'bold'))
            button.pack(fill='x', padx=10, pady=2)
            self.nav_buttons[key] = button
        account = self.gateway_auth.get('user') or {}
        tk.Label(self.nav, text=f"登录子账号\n{account.get('username') or '未知'}\n\nv{APP_VERSION}",
                 justify='left', anchor='w', bg=C['nav'], fg=C['nav_muted'], font=(UI_FONT, 10)).pack(side='bottom', fill='x', padx=20, pady=20)
        self.main = tk.Frame(self.shell, bg=C['bg'])
        self.main.pack(side='left', fill='both', expand=True)

    def _navigate(self, view):
        self.current_view = view
        for button in self.nav_buttons.values():
            button.configure(bg=C['nav'], fg=C['ink'])
        if view in self.nav_buttons:
            self.nav_buttons[view].configure(bg=C['nav_active'], fg='#fff')
        if view == 'overview':
            self._show_overview()
        elif view == 'stores':
            self._show_stores()
        elif view == 'diagnostics':
            self._show_diagnostics()
        elif view == 'update':
            self._show_update()
        elif view == 'settings':
            self._show_settings()
        else:
            self._show_events()

    def _clear_main(self):
        for widget in self.main.winfo_children():
            widget.destroy()

    def _header(self, title, subtitle=''):
        header = tk.Frame(self.main, bg=C['surface'], height=88)
        header.pack(fill='x')
        header.pack_propagate(False)
        left = tk.Frame(header, bg=C['surface'])
        left.pack(side='left', padx=27, pady=(13, 10))
        tk.Label(left, text=title, bg=C['surface'], fg=C['ink'], font=(UI_FONT, 18, 'bold')).pack(anchor='w')
        if subtitle:
            tk.Label(left, text=subtitle, bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w', pady=(3, 0))
        return header

    def _selected(self):
        return next((item for item in self.instances if item.get('id') == self.selected_id), None)

    def _state(self, instance):
        state = self.states.get(instance.get('id'))
        if state and state.get('status') in ('starting', 'running', 'login_required', 'error'):
            return state
        account = chrome_account_from_cookie_file(instance_cookie_file(instance['id'], self.pub_id))
        actual = str(account.get('userId') or '')
        expected = str(instance.get('platformShopId') or '')
        if actual and expected and actual == expected:
            return {'status': 'ready', 'hint': 'Chrome 登录店铺已匹配', 'account': account}
        if actual and expected:
            return {'status': 'mismatch', 'hint': 'Chrome 登录店铺与后台店铺不一致', 'account': account}
        return {'status': 'not_logged', 'hint': '尚未登录该店铺的 Chrome', 'account': account}

    def _show_overview(self):
        self.current_view = 'overview'
        self._clear_main()
        self._navigate_button('overview')
        account = self.gateway_auth.get('user') or {}
        self._header('客户端概览', f'子账号：{account.get("username") or "未知"}  ·  客户端版本：v{APP_VERSION}')
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        if not self.instances:
            self._empty_state(body)
            return
        top = tk.Frame(body, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=21, pady=18)
        top.pack(fill='x')
        info = tk.Frame(top, bg=C['surface'])
        info.pack(side='left', fill='x', expand=True)
        running_count = sum(self._state(store).get('status') == 'running' for store in self.instances)
        chrome_count = sum(self._state(store).get('status') == 'ready' for store in self.instances)
        tk.Label(info, text='闲鱼店铺监听', bg=C['surface'], fg=C['ink'], font=(UI_FONT, 20, 'bold')).pack(anchor='w')
        tk.Label(info, text=f'已授权店铺 {len(self.instances)} 家  ·  运行中 {running_count} 家  ·  Chrome 已登录 {chrome_count} 家', bg=C['surface'], fg=C['muted'], font=(UI_FONT, 11)).pack(anchor='w', pady=(7, 0))
        tk.Label(info, text='店铺启停与重新登录请在“店铺实例”中单独操作。', bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w', pady=(4, 0))
        stats = tk.Frame(body, bg=C['bg'])
        stats.pack(fill='x', pady=14)
        for label, value, color in [('接收消息', sum(item['type'] == 'receive' for item in self.events), C['ink']), ('网关上报', sum(item['type'] == 'report' for item in self.events), C['green']), ('订单事件', sum(item['type'] == 'order' for item in self.events), C['yellow_deep'])]:
            card = tk.Frame(stats, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=15, pady=13)
            card.pack(side='left', fill='x', expand=True, padx=(0, 10))
            tk.Label(card, text=label, bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w')
            tk.Label(card, text=str(value), bg=C['surface'], fg=color, font=(UI_FONT, 23, 'bold')).pack(anchor='w', pady=(4, 0))
        panels = tk.Frame(body, bg=C['bg'])
        panels.pack(fill='both', expand=True)
        logs = tk.Frame(panels, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'])
        logs.pack(side='left', fill='both', expand=True, padx=(0, 10))
        self._panel_header(logs, '公共事件', lambda: self._navigate('events'))
        self.overview_log = self._make_log_widget(logs)
        self._render_log_widget(self.overview_log, None)
        right = tk.Frame(panels, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], width=260)
        right.pack(side='left', fill='y')
        right.pack_propagate(False)
        self._panel_header(right, '店铺实例', lambda: self._navigate('stores'))
        configured_store_ids = {int(store.get('storeId') or 0) for store in self.instances}
        for store in self.instances:
            item = tk.Frame(right, bg=C['surface'])
            item.pack(fill='x', padx=14, pady=9)
            status, bg, fg = self._status_style(self._state(store).get('status'))
            tk.Button(item, text=store.get('name') or '未命名店铺', command=lambda: self._navigate('stores'),
                      anchor='w', bg=C['surface'], fg=C['ink'], bd=0, relief='flat',
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
            tk.Button(item, text='添加', command=lambda value=store: self._add_instance(value), bg=C['yellow_soft'], fg=C['ink'],
                      bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 9, 'bold'), padx=7, pady=4).pack(side='right')

    def _navigate_button(self, key):
        for name, button in self.nav_buttons.items():
            button.configure(bg=C['nav_active'] if name == key else C['nav'], fg='#fff' if name == key else C['ink'])

    def _panel_header(self, parent, title, action):
        row = tk.Frame(parent, bg=C['surface'], height=45)
        row.pack(fill='x')
        row.pack_propagate(False)
        tk.Label(row, text=title, bg=C['surface'], fg=C['ink'], font=(UI_FONT, 11, 'bold')).pack(side='left', padx=15, pady=13)
        tk.Button(row, text='查看全部', command=action, bg=C['surface'], fg=C['yellow_deep'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 9, 'bold')).pack(side='right', padx=11)

    def _make_log_widget(self, parent):
        frame = tk.Frame(parent, bg=C['surface'])
        frame.pack(fill='both', expand=True)
        text = tk.Text(frame, bg=C['surface'], fg=C['ink'], relief='flat', bd=0, wrap='word', padx=15, pady=8, font=(UI_FONT, 10), state='disabled')
        text.tag_configure('time', foreground=C['muted'])
        text.tag_configure('error', foreground=C['red'])
        text.tag_configure('ok', foreground=C['green'])
        text.tag_configure('order', foreground='#0d9488')
        scroll = tk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        return text

    def _show_events(self):
        self.current_view = 'events'
        self._clear_main()
        self._navigate_button('events')
        header = self._header('实时事件', '仅显示对业务有帮助的消息、任务、订单与异常')
        tk.Button(header, text='清空当前日志', command=self._clear_events, bg=C['surface'], fg=C['yellow_deep'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold')).pack(side='right', padx=23)
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        self.events_log = self._make_log_widget(body)
        self._render_log_widget(self.events_log, None)

    def _show_stores(self):
        self.current_view = 'stores'
        self._clear_main()
        self._navigate_button('stores')
        header = self._header('店铺实例', '每个实例使用独立的 Cookie、Chrome Profile、连接与日志')
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        for instance in self.instances:
            self._instance_row(body, instance, configured=True)
        if not self.instances:
            self._empty_state(body)

    def _instance_row(self, parent, instance, configured=True):
        row = tk.Frame(parent, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=18, pady=14)
        row.pack(fill='x', pady=4)
        state = self._state(instance)
        tk.Label(row, text=instance.get('name') or '未命名店铺', bg=C['surface'], fg=C['ink'], font=(UI_FONT, 13, 'bold')).grid(row=0, column=0, sticky='w')
        tk.Label(row, text=f"后台店铺 #{instance.get('storeId') or '未绑定'}  ·  闲鱼 ID：{instance.get('platformShopId') or '待网关升级'}", bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).grid(row=1, column=0, sticky='w', pady=(5, 0))
        hint_color = C['red'] if state.get('status') == 'login_required' else C['muted']
        tk.Label(row, text=state.get('hint') or '', bg=C['surface'], fg=hint_color, font=(UI_FONT, 9)).grid(row=2, column=0, sticky='w', pady=(4, 0))
        row.grid_columnconfigure(0, weight=1)
        status, bg, fg = self._status_style(state.get('status'))
        tk.Label(row, text=status, bg=bg, fg=fg, font=(UI_FONT, 9, 'bold'), padx=8, pady=4).grid(row=0, column=1, rowspan=3, padx=8)
        toggle_text = '停止运行' if state.get('status') in ('running', 'login_required') else '启动'
        tk.Button(row, text=toggle_text, command=lambda item=instance: self._toggle_instance(item), bg=C['yellow'] if toggle_text == '启动' else C['ink'], fg='#fff', bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold'), padx=13, pady=7).grid(row=0, column=2, rowspan=3, padx=(3, 0))
        tk.Button(row, text='重新登录', command=lambda item=instance: self._force_relogin(item), bg=C['surface'], fg=C['yellow_deep'], bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 10, 'bold')).grid(row=0, column=3, rowspan=3, padx=(8, 0))

    def _empty_state(self, parent):
        tk.Label(parent, text='还没有可运行的闲鱼店铺', bg=C['bg'], fg=C['ink'], font=(UI_FONT, 17, 'bold')).pack(pady=(80, 8))
        tk.Label(parent, text='请检查子账号的闲鱼店铺授权，或在“店铺实例”中添加店铺。', bg=C['bg'], fg=C['muted'], font=(UI_FONT, 11)).pack()

    def _add_instance(self, store):
        instance = new_instance(store)
        self.instances.append(instance)
        self.selected_id = instance['id']
        save_instances(self.instances, self.pub_id)
        self._event(instance, 'system', f'已添加店铺实例：{instance["name"]}')
        self._show_stores()

    def _select_instance(self, instance_id):
        self.selected_id = instance_id
        self._show_overview()

    def _toggle_instance(self, instance):
        if self._state(instance).get('status') in ('running', 'starting', 'login_required'):
            self._stop_instance(instance)
        else:
            self._start_instance(instance)

    def _start_instance(self, instance):
        if int(instance.get('storeId') or 0) <= 0:
            messagebox.showwarning('无法启动', '该实例尚未绑定后台闲鱼店铺。请重新登录或联系管理员配置店铺授权。', parent=self.root)
            return
        instance_id = instance['id']
        state = self.states.setdefault(instance_id, {})
        if state.get('status') in ('starting', 'running', 'login_required'):
            return
        previous_thread = self.run_threads.get(instance_id)
        if previous_thread is not None and previous_thread.is_alive():
            state.update({'status': 'stopping', 'hint': '上一个监听正在退出，请稍候'})
            self._refresh_current_view()
            return
        if previous_thread is not None:
            self.run_threads.pop(instance_id, None)

        generation = self.run_generations.get(instance_id, 0) + 1
        self.run_generations[instance_id] = generation
        state.update({'status': 'starting', 'hint': '正在校验 Chrome 登录店铺', 'account': {}})
        self._event(instance, 'system', '实例正在启动，校验 Chrome 登录态')
        thread = threading.Thread(
            target=self._run_instance,
            args=(instance, generation),
            daemon=True,
            name=f'fish-{instance_id}',
        )
        self.run_threads[instance_id] = thread
        thread.start()
        self._refresh_current_view()

    def _is_current_run(self, instance_id, generation, thread):
        return (
            self.run_generations.get(instance_id) == generation
            and self.run_threads.get(instance_id) is thread
        )

    def _run_instance(self, instance, generation):
        from goofish_live import XianyuLive
        instance_id = instance['id']
        current_thread = threading.current_thread()
        port = 18000 + (self.instances.index(instance) if instance in self.instances else 0)
        live = None
        try:
            with logger.contextualize(instance_id=instance_id):
                live = XianyuLive(
                    cookie_file=instance_cookie_file(instance_id, self.pub_id),
                    gateway_auth=self.gateway_auth,
                    gateway_auth_manager=self.gateway_auth_manager,
                    account_changed_callback=lambda account, item=instance: self._chrome_account_changed(item, account),
                    login_state_callback=lambda status, hint, item=instance, run=generation: self._login_state_changed(item, run, status, hint),
                    store_id=instance['storeId'], instance_id=instance_id, instance_name=instance.get('name') or '',
                    chrome_profile_dir=instance_chrome_profile_dir(instance_id, self.pub_id),
                    local_api_port=port, log_dir=instance_log_dir(instance_id, self.pub_id),
                )
                if (
                    self.states.get(instance_id, {}).get('status') == 'stopping'
                    or not self._is_current_run(instance_id, generation, current_thread)
                ):
                    live.stop()
                    return
                self.lives[instance_id] = live
                if not live.ensure_login_with_retry():
                    raise RuntimeError('未能获取有效的闲鱼登录态')
                if live._stop_event.is_set() or not self._is_current_run(instance_id, generation, current_thread):
                    return
                account = live.current_chrome_account()
                expected = str(instance.get('platformShopId') or '').strip()
                actual = str(account.get('userId') or '').strip()
                if not actual:
                    raise RuntimeError('未能从 Chrome Cookie 获取闲鱼店铺 ID')
                if not expected:
                    expected = self._bind_platform_shop_id(instance, actual)
                if expected and actual != expected:
                    raise RuntimeError(f'Chrome 登录闲鱼 ID {actual or "未识别"} 与后台店铺 ID {expected} 不一致')
                self.states[instance_id].update({'status': 'running', 'hint': '正在监听消息', 'account': account})
                self._event(instance, 'system', f'店铺校验成功，开始监听：{account.get("nick") or actual}')
                asyncio.run(live.main())
        except Exception as exc:
            if self._is_current_run(instance_id, generation, current_thread):
                state = self.states.setdefault(instance_id, {})
                if state.get('status') != 'stopping':
                    state.update({'status': 'error', 'hint': str(exc)})
                    self._event(instance, 'error', str(exc))
        finally:
            if live is not None:
                live.stop_local_api()
            if self.lives.get(instance_id) is live:
                self.lives.pop(instance_id, None)
            if self.run_threads.get(instance_id) is current_thread:
                self.run_threads.pop(instance_id, None)
                if self.states.get(instance_id, {}).get('status') != 'error':
                    self.states.setdefault(instance_id, {}).update({'status': 'stopped', 'hint': '已停止'})
            self.root.after(0, self._refresh_current_view)

    def _stop_instance(self, instance):
        instance_id = instance['id']
        live = self.lives.get(instance_id)
        if live is not None:
            live.stop()
        thread = self.run_threads.get(instance_id)
        stopping = thread is not None and thread.is_alive()
        self.states.setdefault(instance_id, {}).update({
            'status': 'stopping' if stopping else 'stopped',
            'hint': '正在停止' if stopping else '已停止',
        })
        self._event(instance, 'system', '已停止监听')
        self._refresh_current_view()

    def _force_relogin(self, instance):
        status = self._state(instance).get('status')
        if status == 'login_required':
            live = self.lives.get(instance['id'])
            if live is not None:
                live.request_login_retry()
                self.states.setdefault(instance['id'], {}).update({
                    'status': 'login_required',
                    'hint': '正在重新尝试登录当前店铺',
                })
                self._event(instance, 'system', '已立即重试当前店铺登录')
                self._refresh_current_view()
                return
        if status in ('running', 'starting', 'login_required'):
            messagebox.showinfo('重新登录', '请先停止该店铺实例，再重新登录。', parent=self.root)
            return
        cookie_path = instance_cookie_file(instance['id'], self.pub_id)
        try:
            os.remove(cookie_path)
        except FileNotFoundError:
            pass
        self._event(instance, 'system', '已清除当前店铺 Cookie，下次启动会打开独立 Chrome 登录')
        self._refresh_current_view()

    def _chrome_account_changed(self, instance, account):
        self.states.setdefault(instance['id'], {}).setdefault('account', {}).update(account or {})
        self.root.after(0, self._refresh_current_view)

    def _login_state_changed(self, instance, generation, status, hint):
        self.root.after(0, lambda: self._apply_login_state(instance, generation, status, hint))

    def _apply_login_state(self, instance, generation, status, hint):
        instance_id = instance['id']
        if self.run_generations.get(instance_id) != generation:
            return
        state = self.states.setdefault(instance_id, {})
        if state.get('status') == 'stopping':
            return
        previous_status = state.get('status')
        state.update({'status': status, 'hint': hint})
        self._refresh_current_view()
        if status == 'login_required' and previous_status != 'login_required':
            messagebox.showwarning(
                '闲鱼需要登录',
                f'{instance.get("name") or "闲鱼店铺"}的登录态已失效。\n\n'
                f'{hint}\n请切换到已打开的独立 Chrome 窗口完成登录。',
                parent=self.root,
            )

    def _bind_platform_shop_id(self, instance, platform_shop_id):
        from ws_client import GatewayTokenError, gateway_bind_xianyu_store

        token = str(self.gateway_auth.get('accessToken') or '')
        try:
            result = gateway_bind_xianyu_store(token, instance['storeId'], platform_shop_id)
        except GatewayTokenError:
            refreshed = self.gateway_auth_manager.refresh(stale_token=token, force=True)
            result = gateway_bind_xianyu_store(
                str(refreshed.get('accessToken') or ''),
                instance['storeId'],
                platform_shop_id,
            )

        bound_shop_id = str(result.get('platformShopId') or '').strip()
        if not bound_shop_id or bound_shop_id != str(platform_shop_id):
            raise RuntimeError('后台返回的闲鱼店铺 ID 与当前登录账号不一致')

        access_token = str(result.get('accessToken') or '')
        if access_token:
            self.gateway_auth['token'] = access_token
            self.gateway_auth['accessToken'] = access_token
        if isinstance(result.get('scope'), dict):
            self.gateway_auth['scope'] = result['scope']
        instance['platformShopId'] = bound_shop_id
        for store in (self.gateway_auth.get('scope') or {}).get('stores') or []:
            if int(store.get('id') or 0) == int(instance.get('storeId') or 0):
                store['platformShopId'] = bound_shop_id
                break
        save_instances(self.instances, self.pub_id)
        self._event(instance, 'system', f'已自动绑定闲鱼店铺 ID：{bound_shop_id}')
        return bound_shop_id

    def _status_style(self, status):
        if status == 'running':
            return '● 运行中', C['green_soft'], C['green']
        if status == 'starting':
            return '● 正在启动', C['yellow_soft'], C['yellow_deep']
        if status == 'login_required':
            return '● 需要登录', C['red_soft'], C['red']
        if status == 'stopping':
            return '● 正在停止', C['gray_soft'], C['muted']
        if status == 'ready':
            return '● Chrome 已登录', C['green_soft'], C['green']
        if status == 'mismatch':
            return '● 登录店铺不一致', C['red_soft'], C['red']
        if status == 'error':
            return '● 启动失败', C['red_soft'], C['red']
        return '○ 未登录', C['gray_soft'], C['muted']

    def _show_diagnostics(self):
        self.current_view = 'diagnostics'
        self._clear_main()
        self._navigate_button('diagnostics')
        self._render_diagnostics_page()

    def _render_diagnostics_page(self):
        header = self._header('状态检测', '检查本机环境、店铺连接与近期任务状态')
        header_actions = tk.Frame(header, bg=C['surface'])
        header_actions.pack(side='right', padx=23, pady=14)
        checked_at = self._format_diagnostics_time(
            (self.diagnostics_result or {}).get('checkedAt') if isinstance(self.diagnostics_result, dict) else ''
        )
        tk.Label(
            header_actions,
            text=f'上次检测：{checked_at}' if checked_at else '尚未检测',
            bg=C['surface'], fg=C['muted'], font=(UI_FONT, 9),
        ).pack(anchor='e', pady=(0, 6))
        self.diagnostics_button = tk.Button(
            header_actions,
            text='正在检测...' if self.diagnostics_running else '立即检测',
            command=self._start_status_diagnostics,
            state='disabled' if self.diagnostics_running else 'normal',
            bg=C['gray_soft'] if self.diagnostics_running else C['yellow'],
            fg=C['muted'] if self.diagnostics_running else '#fff',
            activebackground=C['yellow_deep'], activeforeground='#fff',
            bd=0, relief='flat', cursor='arrow' if self.diagnostics_running else 'hand2',
            font=(UI_FONT, 10, 'bold'), padx=15, pady=8,
        )
        self.diagnostics_button.pack(anchor='e')

        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=18)
        content = self._make_diagnostics_scroll_body(body)
        if self.diagnostics_running:
            self._diagnostics_notice(content, '正在检测', '正在读取本机和店铺状态，请稍候。', C['yellow_soft'], C['yellow_deep'])
        elif self.diagnostics_error:
            self._diagnostics_notice(content, '检测未完成', self.diagnostics_error, C['red_soft'], C['red'])
        elif isinstance(self.diagnostics_result, dict):
            self._render_diagnostics_result(content, self.diagnostics_result)
        else:
            self._diagnostics_notice(
                content,
                '尚未执行状态检测',
                '点击“立即检测”后查看本机环境和各店铺的当前状态。',
                C['surface'], C['muted'],
            )

    @staticmethod
    def _make_diagnostics_scroll_body(parent):
        canvas = tk.Canvas(parent, bg=C['bg'], highlightthickness=0)
        scrollbar = tk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        content = tk.Frame(canvas, bg=C['bg'])
        window = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind(
            '<MouseWheel>',
            lambda event: canvas.yview_scroll(-3 if event.delta > 0 else 3, 'units'),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        return content

    @staticmethod
    def _diagnostics_notice(parent, title, detail, bg, fg):
        panel = tk.Frame(
            parent, bg=bg, highlightthickness=1, highlightbackground=C['line'], padx=22, pady=20,
        )
        panel.pack(fill='x', pady=(0, 10))
        tk.Label(panel, text=title, bg=bg, fg=fg, font=(UI_FONT, 14, 'bold')).pack(anchor='w')
        tk.Label(
            panel, text=str(detail), bg=bg, fg=C['ink'] if fg != C['muted'] else C['muted'],
            justify='left', anchor='w', wraplength=760, font=(UI_FONT, 10),
        ).pack(fill='x', pady=(7, 0))

    def _render_diagnostics_result(self, parent, result):
        self._render_diagnostics_summary(parent, result)
        for group in self._diagnostic_groups(result):
            self._render_diagnostics_group(parent, group)

    def _render_diagnostics_summary(self, parent, result):
        status = str(result.get('status') or 'unknown')
        label, bg, fg = self._diagnostic_status_style(status)
        counts = result.get('counts') if isinstance(result.get('counts'), dict) else {}
        panel = tk.Frame(
            parent, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=20, pady=17,
        )
        panel.pack(fill='x', pady=(0, 10))
        top = tk.Frame(panel, bg=C['surface'])
        top.pack(fill='x')
        tk.Label(top, text='总体结论', bg=C['surface'], fg=C['ink'], font=(UI_FONT, 13, 'bold')).pack(side='left')
        tk.Label(top, text=label, bg=bg, fg=fg, font=(UI_FONT, 9, 'bold'), padx=8, pady=4).pack(side='right')
        count_row = tk.Frame(panel, bg=C['surface'])
        count_row.pack(fill='x', pady=(13, 8))
        for key, title, color in (
            ('normal', '正常', C['green']),
            ('warning', '提醒', C['yellow_deep']),
            ('error', '异常', C['red']),
            ('unknown', '未完成', C['muted']),
        ):
            block = tk.Frame(count_row, bg=C['surface'])
            block.pack(side='left', padx=(0, 28))
            tk.Label(block, text=str(int(counts.get(key) or 0)), bg=C['surface'], fg=color,
                     font=(UI_FONT, 18, 'bold')).pack(anchor='w')
            tk.Label(block, text=title, bg=C['surface'], fg=C['muted'], font=(UI_FONT, 9)).pack(anchor='w')
        issue = self._most_important_diagnostic(result)
        if issue:
            tk.Label(
                panel, text=f"优先关注：{issue.get('title') or '状态异常'} - {issue.get('detail') or ''}",
                bg=C['surface'], fg=C['ink'], justify='left', anchor='w', wraplength=780,
                font=(UI_FONT, 10),
            ).pack(fill='x', pady=(4, 0))

    def _render_diagnostics_group(self, parent, group):
        items = group.get('items') if isinstance(group.get('items'), list) else []
        status = self._diagnostic_group_status(items)
        label, bg, fg = self._diagnostic_status_style(status)
        panel = tk.Frame(parent, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'])
        panel.pack(fill='x', pady=5)
        heading = tk.Frame(panel, bg=C['surface'])
        heading.pack(fill='x', padx=18, pady=(14, 9))
        tk.Label(heading, text=group.get('title') or '检测结果', bg=C['surface'], fg=C['ink'],
                 font=(UI_FONT, 12, 'bold')).pack(side='left')
        tk.Label(heading, text=label, bg=bg, fg=fg, font=(UI_FONT, 8, 'bold'), padx=7, pady=3).pack(side='right')
        instance = self._diagnostic_instance(group.get('store'))
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if index:
                tk.Frame(panel, bg=C['line'], height=1).pack(fill='x', padx=18)
            self._render_diagnostics_item(panel, item, instance)

    def _render_diagnostics_item(self, parent, item, instance):
        row = tk.Frame(parent, bg=C['surface'])
        row.pack(fill='x', padx=18, pady=10)
        label, bg, fg = self._diagnostic_status_style(str(item.get('status') or 'unknown'))
        tk.Label(row, text=label, width=7, bg=bg, fg=fg, font=(UI_FONT, 8, 'bold'),
                 padx=5, pady=3).pack(side='left', anchor='n', padx=(0, 11))
        text = tk.Frame(row, bg=C['surface'])
        text.pack(side='left', fill='x', expand=True)
        tk.Label(text, text=item.get('title') or '检测项', bg=C['surface'], fg=C['ink'],
                 font=(UI_FONT, 10, 'bold')).pack(anchor='w')
        tk.Label(
            text, text=str(item.get('detail') or ''), bg=C['surface'], fg=C['muted'],
            justify='left', anchor='w', wraplength=620, font=(UI_FONT, 9),
        ).pack(fill='x', pady=(4, 0))
        action = str(item.get('action') or '')
        if action in DIAGNOSTIC_ACTION_LABELS:
            tk.Button(
                row, text=DIAGNOSTIC_ACTION_LABELS[action],
                command=lambda value=action, store=instance, selected=item:
                    self._run_diagnostics_action(value, store, selected),
                bg=C['surface'], fg=C['yellow_deep'], activebackground=C['yellow_soft'],
                bd=0, relief='flat', cursor='hand2', font=(UI_FONT, 9, 'bold'), padx=8, pady=4,
            ).pack(side='right', anchor='n', padx=(10, 0))

    def _start_status_diagnostics(self):
        if self.diagnostics_running:
            return
        self.diagnostics_running = True
        self.diagnostics_result = None
        self.diagnostics_error = ''
        if self.current_view == 'diagnostics':
            self._show_diagnostics()
        threading.Thread(
            target=self._status_diagnostics_worker,
            daemon=True,
            name='status-diagnostics',
        ).start()

    def _status_diagnostics_worker(self):
        result = None
        error = None
        try:
            result = StatusDiagnostics(
                self.gateway_auth_manager,
                self.gateway_auth,
                list(self.instances),
                dict(self.lives),
                dict(self.run_threads),
                self.pub_id,
            ).run()
        except Exception as exc:
            error = exc
        self.root.after(
            0,
            lambda value=result, failure=error: self._finish_status_diagnostics(value, failure),
        )

    def _finish_status_diagnostics(self, result, error):
        self.diagnostics_running = False
        if error is not None:
            self.diagnostics_result = None
            self.diagnostics_error = str(error) or '状态检测发生未知错误'
        elif isinstance(result, dict):
            self.diagnostics_result = result
            self.diagnostics_error = ''
        else:
            self.diagnostics_result = None
            self.diagnostics_error = '状态检测未返回有效结果'
        if self.current_view == 'diagnostics':
            self._show_diagnostics()

    def _run_diagnostics_action(self, action, instance, item):
        if action not in DIAGNOSTIC_ACTION_LABELS:
            return
        if action == 'start_instance' and instance is not None:
            self._start_instance(instance)
            return
        if action == 'relogin' and instance is not None:
            self._force_relogin(instance)
            return
        if action == 'check_update':
            self._navigate('update')
            self._start_update_check()
            return
        if action == 'view_advice':
            item = item if isinstance(item, dict) else {}
            messagebox.showinfo(
                item.get('title') or '处理建议',
                str(item.get('detail') or '请稍后重新检测，或查看运行日志确认具体原因。'),
                parent=self.root,
            )

    def _diagnostic_instance(self, store):
        if not isinstance(store, dict):
            return None
        instance_id = str(store.get('instanceId') or '')
        if instance_id:
            matched = next((item for item in self.instances if str(item.get('id') or '') == instance_id), None)
            if matched is not None:
                return matched
        try:
            store_id = int(store.get('storeId') or 0)
        except (TypeError, ValueError):
            return None
        return next((item for item in self.instances if int(item.get('storeId') or 0) == store_id), None)

    @staticmethod
    def _diagnostic_groups(result):
        if not isinstance(result, dict):
            return []
        local_items = [item for item in (result.get('local') or []) if isinstance(item, dict)]
        gateway = result.get('gateway')
        if isinstance(gateway, dict):
            local_items.append(gateway)
        groups = [{'key': 'local', 'title': '本机环境', 'items': local_items, 'store': None}]
        for index, store in enumerate(result.get('stores') or []):
            if not isinstance(store, dict):
                continue
            instance_id = str(store.get('instanceId') or '')
            groups.append({
                'key': instance_id or f'store-{index}',
                'title': store.get('name') or f"店铺 #{store.get('storeId') or '未绑定'}",
                'items': [item for item in (store.get('items') or []) if isinstance(item, dict)],
                'store': store,
            })
        return groups

    @staticmethod
    def _diagnostic_group_status(items):
        statuses = {str(item.get('status') or 'unknown') for item in items if isinstance(item, dict)}
        if 'error' in statuses:
            return 'error'
        if 'warning' in statuses:
            return 'warning'
        if 'unknown' in statuses:
            return 'unknown'
        return 'normal'

    @staticmethod
    def _most_important_diagnostic(result):
        items = []
        for group in XianyuDesktopApp._diagnostic_groups(result):
            items.extend(group['items'])
        for status in ('error', 'warning', 'unknown'):
            matched = next((item for item in items if item.get('status') == status), None)
            if matched is not None:
                return matched
        return None

    @staticmethod
    def _diagnostic_status_style(status):
        if status == 'normal':
            return '正常', C['green_soft'], C['green']
        if status == 'warning':
            return '提醒', C['yellow_soft'], C['yellow_deep']
        if status == 'error':
            return '异常', C['red_soft'], C['red']
        return '未完成', C['gray_soft'], C['muted']

    @staticmethod
    def _format_diagnostics_time(value):
        if not value:
            return ''
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            return str(value)

    def _show_update(self):
        self.current_view = 'update'
        self._clear_main()
        self._navigate_button('update')
        self._header('软件更新', '检查、下载并安装最新客户端版本')
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        card = tk.Frame(body, bg=C['surface'], highlightthickness=1, highlightbackground=C['line'], padx=24, pady=22)
        card.pack(fill='x')
        tk.Label(card, text='闲鱼店铺插件', bg=C['surface'], fg=C['ink'], font=(UI_FONT, 18, 'bold')).pack(anchor='w')
        tk.Label(card, text=f'当前版本：v{APP_VERSION}', bg=C['surface'], fg=C['muted'], font=(UI_FONT, 11)).pack(anchor='w', pady=(9, 0))
        tk.Label(card, text='更新会下载经校验的 Windows 安装包，现有店铺 Cookie 与配置会被保留。', bg=C['surface'], fg=C['muted'], font=(UI_FONT, 10)).pack(anchor='w', pady=(5, 17))
        
        btn_row = tk.Frame(card, bg=C['surface'])
        btn_row.pack(anchor='w')
        self.update_check_btn = tk.Button(
            btn_row,
            text='检查更新' if not self.downloading_update else '正在下载更新...',
            command=self._start_update_check,
            bg=C['yellow'] if not self.downloading_update else C['gray_soft'],
            fg='#fff' if not self.downloading_update else C['muted'],
            activebackground=C['yellow_deep'], activeforeground='#fff',
            state='disabled' if self.downloading_update else 'normal',
            bd=0, relief='flat', cursor='hand2' if not self.downloading_update else 'arrow',
            font=(UI_FONT, 11, 'bold'), padx=18, pady=10,
        )
        self.update_check_btn.pack(side='left')

        if self.downloading_update and self.update_progress_info:
            progress_card = tk.Frame(card, bg=C['surface'])
            progress_card.pack(fill='x', pady=(16, 0))
            v = self.update_progress_info.get('version', '')
            down = self.update_progress_info.get('downloaded', 0)
            total = self.update_progress_info.get('total', 0)
            self.update_progress_label = tk.Label(
                progress_card,
                text='',
                bg=C['surface'], fg=C['green'], font=(UI_FONT, 11, 'bold'),
            )
            self.update_progress_label.pack(anchor='w')
            self._refresh_update_progress_text(v, down, total, self.update_progress_info.get('verifying', False))

    def _show_settings(self):
        self.current_view = 'settings'
        self._clear_main()
        self._navigate_button('settings')
        self._header('设置', '客户端设置将在后续版本逐步开放')
        body = tk.Frame(self.main, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=25, pady=23)
        tk.Label(body, text='暂无可配置项', bg=C['bg'], fg=C['muted'], font=(UI_FONT, 12)).pack(anchor='w', pady=(28, 0))

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
        return any(key in text for key in (
            '收到买家', '上报网关', '网关任务', '发送消息', '订单', '登录', '连接', '失败', '异常', '校验',
            '闲鱼 WS', '闲鱼同步', '闲鱼收到', '解析', '网关绑定成功', 'WS 服务端消息',
        ))

    @staticmethod
    def _event_type(text):
        if '失败' in text or '异常' in text or '不一致' in text:
            return 'error'
        if '订单' in text:
            return 'order'
        if '上报网关' in text:
            return 'report'
        if '收到' in text or '同步推送' in text:
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
        if self.current_view == 'overview':
            self._show_overview()
        elif self.current_view == 'events':
            self._show_events()
        elif self.current_view == 'stores':
            self._show_stores()
        elif self.current_view == 'diagnostics':
            self._show_diagnostics()
        elif self.current_view == 'update':
            self._show_update()
        elif self.current_view == 'settings':
            self._show_settings()

    def _clear_events(self):
        self.events = []
        self._refresh_logs_only()

    def _start_update_check(self):
        if self.downloading_update:
            return
        if hasattr(self, 'update_check_btn') and self.update_check_btn.winfo_exists():
            self.update_check_btn.configure(state='disabled', text='正在检查...')
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            info = updater.check_for_update(APP_VERSION)
            self.root.after(0, lambda: self._finish_update_check(info, None))
        except Exception as exc:
            self.root.after(0, lambda: self._finish_update_check(None, exc))

    def _finish_update_check(self, info, error):
        if hasattr(self, 'update_check_btn') and self.update_check_btn.winfo_exists():
            self.update_check_btn.configure(state='normal', text='检查更新')
        if error:
            messagebox.showwarning('检查更新', f'检查更新失败：{error}', parent=self.root)
            return
        if info is None:
            messagebox.showinfo('检查更新', f'当前已是最新版本 (v{APP_VERSION})', parent=self.root)
            return
        size_mb = (info.installer_size / (1024 * 1024)) if info.installer_size > 0 else 0
        size_hint = f' (大小: {size_mb:.1f} MB)' if size_mb > 0 else ''
        if messagebox.askyesno('发现新版本', f'发现新版本 v{info.version}{size_hint}，现在下载并安装吗？', parent=self.root):
            self.downloading_update = True
            self.update_progress_info = {
                'version': info.version,
                'downloaded': 0,
                'total': info.installer_size,
                'verifying': False,
            }
            self.download_dialog = DownloadProgressDialog(self.root, info.version, info.installer_size)
            self._show_update()
            threading.Thread(target=self._download_update_worker, args=(info,), daemon=True).start()

    def _download_update_worker(self, info):
        def on_progress(downloaded, total):
            self.root.after(0, lambda d=downloaded, t=total: self._on_download_progress(d, t))

        def on_verifying():
            self.root.after(0, self._on_download_verifying)

        try:
            installer = updater.download_installer(
                info,
                progress_callback=on_progress,
                verifying_callback=on_verifying,
            )
            self.root.after(0, lambda: self._finish_update_download(installer, None))
        except Exception as exc:
            self.root.after(0, lambda: self._finish_update_download(None, exc))

    def _on_download_progress(self, downloaded, total):
        if self.update_progress_info is not None:
            self.update_progress_info.update({'downloaded': downloaded, 'total': total, 'verifying': False})
        if self.download_dialog:
            self.download_dialog.update_progress(downloaded, total)
        if self.update_progress_info:
            self._refresh_update_progress_text(
                self.update_progress_info['version'],
                downloaded,
                total,
                False,
            )

    def _on_download_verifying(self):
        if self.update_progress_info is not None:
            self.update_progress_info['verifying'] = True
        if self.download_dialog:
            self.download_dialog.set_verifying()
        if self.update_progress_info:
            self._refresh_update_progress_text(
                self.update_progress_info['version'],
                self.update_progress_info.get('downloaded', 0),
                self.update_progress_info.get('total', 0),
                True,
            )

    def _refresh_update_progress_text(self, version, downloaded, total, verifying):
        label = getattr(self, 'update_progress_label', None)
        if not label or not label.winfo_exists():
            return
        if verifying:
            label.configure(text=f'更新 v{version} 已下载完成，正在校验完整性...')
            return
        if total > 0:
            pct = min(100.0, downloaded / total * 100.0)
            label.configure(text=f'正在下载更新 v{version}：{pct:.1f}% ({downloaded / (1024 * 1024):.1f} MB / {total / (1024 * 1024):.1f} MB)')
            return
        label.configure(text=f'正在下载更新 v{version}：已下载 {downloaded / (1024 * 1024):.1f} MB')

    def _finish_update_download(self, installer, error):
        self.downloading_update = False
        if self.download_dialog:
            self.download_dialog.close()
            self.download_dialog = None
        self._refresh_current_view()
        if error:
            messagebox.showwarning('下载更新', f'下载更新失败：{error}', parent=self.root)
            return
        if messagebox.askyesno('安装更新', '更新包已下载并校验完成，是否现在安装？（安装会关闭当前客户端）', parent=self.root):
            try:
                updater.launch_installer(installer)
            except Exception as exc:
                messagebox.showwarning(
                    '安装更新',
                    f'启动安装程序失败：{exc}\n\n安装包位置：{installer}\n请打开该目录后手动运行安装包。',
                    parent=self.root,
                )
                return
            self._on_close()

    def _on_close(self):
        for instance in list(self.instances):
            self._stop_instance(instance)
        try:
            logger.remove(self._sink_id)
        except ValueError:
            pass
        self.root.destroy()
