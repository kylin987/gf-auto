import unittest
from unittest.mock import Mock

from goofish_apis import XianyuApis
from goofish_live import XianyuLive
from ws_client import GatewayClient


class SellerGoodsSyncTest(unittest.TestCase):
    def _live(self, responses):
        live = XianyuLive.__new__(XianyuLive)
        live.xianyu = type('Api', (), {})()
        live.xianyu.search_seller_items = lambda page, size: responses[page - 1]
        live._call_seller_api_with_token_retry = lambda action, request: request()
        return live

    @staticmethod
    def _response(page, total_pages, total, items):
        return {
            'ret': ['SUCCESS::调用成功'],
            'data': {
                'code': 'success',
                'data': {
                    'currentPage': page,
                    'hasNextPage': page < total_pages,
                    'itemSearchResponseList': items,
                    'success': True,
                    'total': total,
                    'totalPage': total_pages,
                },
            },
        }

    def test_fetches_all_pages_and_normalizes_items(self):
        live = self._live([
            self._response(1, 2, 2, [{
                'itemId': '101', 'title': '商品A', 'reservePrice': '9.90',
                'itemStatus': 0, 'itemImageUrl': 'https://example.test/a.jpg', 'quantity': 9,
            }]),
            self._response(2, 2, 2, [{
                'itemId': '102', 'title': '商品B', 'reservePrice': '19.90',
                'itemStatus': -9, 'itemImageUrl': 'https://example.test/b.jpg', 'quantity': 3,
            }]),
        ])

        result = live.get_seller_goods({'pageSize': 20})

        self.assertEqual(result['total'], 2)
        self.assertEqual(result['pages'], 2)
        self.assertEqual([item['goodsId'] for item in result['items']], ['101', '102'])
        self.assertEqual(result['items'][1]['status'], -9)

    def test_rejects_partial_page_result(self):
        live = self._live([
            self._response(1, 1, 2, [{'itemId': '101', 'title': '商品A'}]),
        ])

        with self.assertRaisesRegex(RuntimeError, '列表不完整'):
            live.get_seller_goods({})

    def test_rejects_failed_page(self):
        live = self._live([
            {'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期']},
        ])

        with self.assertRaisesRegex(RuntimeError, '查询闲鱼商品失败'):
            live.get_seller_goods({})

    def test_seller_item_request_matches_captured_api(self):
        api = XianyuApis.__new__(XianyuApis)
        api._seller_mtop_post = Mock(return_value={'ret': ['SUCCESS::调用成功']})

        api.search_seller_items(2, 20)

        args, kwargs = api._seller_mtop_post.call_args
        self.assertEqual(args[0], 'mtop.alibaba.idle.seller.pc.common.item.search')
        self.assertIn('"pageNo":2', args[1])
        self.assertIn('"itemStatus":"0,-9"', args[1])
        self.assertEqual(kwargs['query_params']['needLoginPC'], 'true')

    def test_gateway_task_routes_to_local_goods_api(self):
        client = GatewayClient.__new__(GatewayClient)
        client._local_post = Mock(return_value={
            'ok': True,
            'result': {'total': 0, 'pages': 1, 'items': []},
        })

        result = client._execute_task('task.xianyu.sync_goods', {'pageSize': 20})

        self.assertTrue(result['ok'])
        client._local_post.assert_called_once_with('/api/goods', {'pageSize': 20})


if __name__ == '__main__':
    unittest.main()
