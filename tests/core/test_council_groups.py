import unittest

from astra_backend.council.groups import (
    MAX_SYMBOLS_PER_GROUP,
    build_group_prompts,
    stable_symbol_groups,
)


MARKET_MARKER = "======================= 【全标的池原生行情、技术指标与筹码矩阵】 ======================="
TIME_MARKER = "======================= 【当前决策时间戳与市场时效】 ======================="
SEPARATOR = "---------------------------------------------------------"


def _package(symbol):
    return {
        "name": symbol,
        "instId": f"{symbol}-USDT-SWAP",
        "type": "crypto",
    }


def _market_block(symbol):
    return f"{SEPARATOR}\n【{symbol} ({symbol}-USDT-SWAP)】| 数据质量: valid\n- 现价: 100\n"


def _full_prompt(symbols):
    body = "\n".join(_market_block(symbol) for symbol in symbols)
    return (
        "市场 regime=RISK_ON\n"
        "账户余额=1234.56 USDT\n"
        "持仓=BTC long\n"
        "挂单=order-42\n"
        "新闻=宏观情报保留\n"
        "风险预算=5%\n\n"
        f"{MARKET_MARKER}\n"
        f"{body}\n\n"
        f"{TIME_MARKER}\n"
        "【推演基准时间】: 2026-10-04 12:00:00\n"
        "【当前账户可用资金】: 1234.56 USDT\n"
        "决策时间戳必须保留\n"
    )


class StableSymbolGroupsTests(unittest.TestCase):
    def test_preserves_package_order_deduplicates_canonical_symbols_and_caps_groups(self):
        packages = [_package("S0"), _package("S1"), _package("S2")]
        packages += [{"name": "s1_usdt", "instId": "S1-USDT-SWAP"}]
        packages += [_package(f"S{i}") for i in range(3, 9)]

        groups = stable_symbol_groups(packages)

        self.assertEqual(
            [[pkg["name"] for pkg in group] for group in groups],
            [
                ["S0", "S1", "S2", "S3", "S4", "S5", "S6"],
                ["S7", "S8"],
            ],
        )
        self.assertTrue(all(len(group) <= MAX_SYMBOLS_PER_GROUP for group in groups))

    def test_rejects_non_positive_group_size(self):
        with self.assertRaisesRegex(ValueError, "max_symbols must be positive"):
            stable_symbol_groups([_package("BTC")], max_symbols=0)


class GroupPromptTests(unittest.TestCase):
    def setUp(self):
        self.packages = [_package("BTC"), _package("ETH")]
        self.packages += [_package(f"S{i}") for i in range(9)]
        self.prompt = _full_prompt([pkg["name"] for pkg in self.packages])

    def test_uses_real_decorated_headings_and_keeps_global_context(self):
        groups = build_group_prompts(self.prompt, self.packages)

        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0].index, 0)
        self.assertEqual(groups[1].index, 1)
        self.assertEqual(len(groups[0].symbols), 7)
        self.assertEqual(len(groups[1].symbols), 2)
        self.assertEqual(groups[0].symbols, tuple(f"S{i}" for i in range(7)))
        self.assertEqual(groups[1].symbols, ("S7", "S8"))
        self.assertEqual(groups[1].reference_symbols, ("BTC", "ETH"))

        first, second = groups[0].prompt, groups[1].prompt
        for text in (
            "市场 regime=RISK_ON",
            "账户余额=1234.56 USDT",
            "持仓=BTC long",
            "挂单=order-42",
            "新闻=宏观情报保留",
            "风险预算=5%",
            TIME_MARKER,
            "【推演基准时间】: 2026-10-04 12:00:00",
            "决策时间戳必须保留",
        ):
            self.assertIn(text, first)
            self.assertIn(text, second)

        self.assertIn(MARKET_MARKER, first)
        self.assertIn(MARKET_MARKER, second)
        self.assertIn("全局参考标的（保留用于市场联动判断，不属于本组主责输出）：BTC, ETH", second)
        self.assertNotIn("【S7 (S7-USDT-SWAP)】", first)
        self.assertIn("【S7 (S7-USDT-SWAP)】", second)
        self.assertIn("【BTC (BTC-USDT-SWAP)】", second)
        self.assertIn("【ETH (ETH-USDT-SWAP)】", second)

    def test_group_matrix_contains_only_primary_symbols_and_available_references(self):
        groups = build_group_prompts(self.prompt, self.packages)

        first, second = groups
        for symbol in ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "BTC", "ETH"):
            self.assertIn(f"【{symbol} ({symbol}-USDT-SWAP)】", first.prompt)
        for symbol in ("S7", "S8"):
            self.assertNotIn(f"【{symbol} ({symbol}-USDT-SWAP)】", first.prompt)

        for symbol in ("S7", "S8", "BTC", "ETH"):
            self.assertIn(f"【{symbol} ({symbol}-USDT-SWAP)】", second.prompt)
        for symbol in ("S0", "S1", "S2", "S3", "S4", "S5", "S6"):
            self.assertNotIn(f"【{symbol} ({symbol}-USDT-SWAP)】", second.prompt)

    def test_missing_decorated_market_section_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "no replaceable market-matrix section"):
            build_group_prompts("没有行情矩阵", self.packages)

    def test_empty_packages_return_no_groups(self):
        self.assertEqual(build_group_prompts(self.prompt, []), ())


if __name__ == "__main__":
    unittest.main()
