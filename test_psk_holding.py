import unittest

from competitor_parsers.psk_holding import parse_sku_page


class PskPriceTests(unittest.TestCase):
    def test_duplicate_title_and_large_menu_do_not_hide_primary_price(self):
        # Prices and markup observed on the five monitored PSK cards.
        for price in (2430, 2460, 3156, 2738, 2625):
            with self.subTest(price=price):
                title = "Фанера ламинированная 18x1220x2440 мм"
                html = (f"<html><head><title>{title} - купить</title></head><body>"
                        + "<nav>Длинное меню каталога</nav>" * 200
                        + f"<h1>{title}</h1><div id='main_price'>"
                        + f"<span class='price_value'>Цена от {price // 1000}\u00a0{price % 1000:03} руб.</span>"
                        + "<meta itemprop='price' content='9999'></div>"
                        + "<aside>Доставка 390 ₽. Похожие товары: Цена от 999 руб.</aside></body></html>")
                self.assertEqual(parse_sku_page(html)["price"], price)

    def test_primary_microdata_fallback(self):
        html = "<div id='main_price'><meta itemprop='price' content='2430.00'></div>"
        self.assertEqual(parse_sku_page(html)["price"], 2430)

    def test_does_not_pick_delivery_or_another_product_price(self):
        html = ("<h1>Фанера</h1><div id='main_price'>Цена по запросу</div>"
                "<aside>Доставка 390 ₽. Цена от 1234 руб.</aside>")
        self.assertIsNone(parse_sku_page(html)["price"])

    def test_zero_or_invalid_primary_metadata_is_not_a_price(self):
        for value in ("0", "invalid"):
            html = f"<div id='main_price'><meta itemprop='price' content='{value}'></div>"
            self.assertIsNone(parse_sku_page(html)["price"])

    def test_jsonld_fallback_is_preserved(self):
        html = '<script type="application/ld+json">{"@type":"Product","offers":{"price":"2738"}}</script>'
        self.assertEqual(parse_sku_page(html)["price"], 2738)


if __name__ == "__main__":
    unittest.main()
