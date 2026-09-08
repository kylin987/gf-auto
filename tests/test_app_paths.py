import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_paths import (
    instance_cookie_file,
    instance_dir,
    instances_file,
    load_instances,
    save_instances,
)


class AppPathsTest(unittest.TestCase):
    def test_legacy_instances_are_migrated_only_to_authorized_pub(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_instances = [
                {'id': 'store-a', 'storeId': 29, 'name': '小影票务'},
                {'id': 'store-b', 'storeId': 203, 'name': '其他店铺'},
            ]
            (root / 'instances.json').write_text(
                json.dumps({'version': 1, 'instances': legacy_instances}, ensure_ascii=False),
                encoding='utf-8',
            )

            with patch('app_paths.app_data_dir', return_value=root):
                pub_a = load_instances(1001, [{'id': 29, 'platformShopId': 'shop-a'}])
                pub_b = load_instances(2002, [{'id': 203, 'platformShopId': 'shop-b'}])
                pub_c = load_instances(3003, [{'id': 888, 'platformShopId': 'shop-c'}])

                self.assertEqual([item['storeId'] for item in pub_a], [29])
                self.assertEqual([item['storeId'] for item in pub_b], [203])
                self.assertEqual(pub_c, [])
                self.assertTrue(instances_file(1001).is_file())
                self.assertTrue(instances_file(2002).is_file())
                self.assertFalse(instances_file(3003).exists())

                save_instances(pub_a, 1001)
                unchanged_legacy = json.loads((root / 'instances.json').read_text(encoding='utf-8'))
                self.assertEqual(unchanged_legacy['instances'], legacy_instances)

    def test_unbound_legacy_instance_requires_matching_platform_shop_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'instances.json').write_text(
                json.dumps({
                    'version': 1,
                    'instances': [{'id': 'legacy-a', 'storeId': 0, 'name': '旧单店实例'}],
                }, ensure_ascii=False),
                encoding='utf-8',
            )
            cookie_dir = root / 'instances' / 'legacy-a'
            cookie_dir.mkdir(parents=True)
            (cookie_dir / 'cookies.json').write_text(
                json.dumps({'cookies': {'unb': 'shop-a'}}, ensure_ascii=False),
                encoding='utf-8',
            )

            with patch('app_paths.app_data_dir', return_value=root):
                matching_pub = load_instances(1001, [{'id': 29, 'platformShopId': 'shop-a'}])
                other_pub = load_instances(2002, [{'id': 203, 'platformShopId': 'shop-b'}])

                self.assertEqual([item['id'] for item in matching_pub], ['legacy-a'])
                self.assertEqual(other_pub, [])
                self.assertTrue(Path(instance_cookie_file('legacy-a', 1001)).is_file())

    def test_saved_pub_instances_are_filtered_by_current_authorized_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved_instances = [
                {'id': 'store-a', 'storeId': 29, 'name': '店铺 A'},
                {'id': 'store-b', 'storeId': 203, 'name': '已删除店铺'},
                {'id': 'store-c', 'storeId': 305, 'name': '店铺 C'},
            ]

            with patch('app_paths.app_data_dir', return_value=root):
                save_instances(saved_instances, 1001)
                active = load_instances(1001, [
                    {'id': 29, 'platformShopId': 'shop-a'},
                    {'id': 305, 'platformShopId': 'shop-c'},
                ])

                self.assertEqual([item['storeId'] for item in active], [29, 305])
                persisted = json.loads(instances_file(1001).read_text(encoding='utf-8'))
                self.assertEqual([item['storeId'] for item in persisted['instances']], [29, 305])

    def test_instance_runtime_data_is_isolated_by_pub(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('app_paths.app_data_dir', return_value=root):
                self.assertEqual(
                    instance_dir('store-a', 1001),
                    root / 'pubs' / '1001' / 'instances' / 'store-a',
                )
                self.assertNotEqual(instance_dir('store-a', 1001), instance_dir('store-a', 2002))

    def test_legacy_runtime_data_migrates_without_overwriting_pub_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_dir = root / 'instances' / 'store-a'
            legacy_dir.mkdir(parents=True)
            (legacy_dir / 'cookies.json').write_text('legacy-cookie', encoding='utf-8')
            (legacy_dir / 'event_outbox.sqlite3').write_text('legacy-outbox', encoding='utf-8')
            (root / 'instances.json').write_text(json.dumps({
                'version': 1,
                'instances': [{'id': 'store-a', 'storeId': 29, 'name': '店铺 A'}],
            }, ensure_ascii=False), encoding='utf-8')

            with patch('app_paths.app_data_dir', return_value=root):
                target_dir = instance_dir('store-a', 1001)
                (target_dir / 'cookies.json').write_text('current-cookie', encoding='utf-8')
                load_instances(1001, [{'id': 29, 'platformShopId': 'shop-a'}])

                self.assertEqual((target_dir / 'cookies.json').read_text(encoding='utf-8'), 'current-cookie')
                self.assertEqual((target_dir / 'event_outbox.sqlite3').read_text(encoding='utf-8'), 'legacy-outbox')


if __name__ == '__main__':
    unittest.main()
