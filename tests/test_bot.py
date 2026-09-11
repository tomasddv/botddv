import copy
import threading
import unittest
from unittest.mock import patch
import pandas as pd

import assistant_engine as bot
import auto_refresh
from sources import frescura_source as fs, planificacion_source as ps
from sources.health import describe, stamp


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(fs, '_snapshot', {'updated_at': stamp(), 'products': {
                '10810': {'codigo': '10810', 'descripcion': 'Producto A', 'stock_trelew': 10, 'stock_madryn': 20, 'stock_total_ddv': 30},
                '30789': {'codigo': '30789', 'descripcion': 'Producto B', 'stock_trelew': 40, 'stock_madryn': 50, 'stock_total_ddv': 90}}}),
            patch.object(ps, '_snapshot', {'updated_at': stamp(), 'customers': {
                '3992': {'id': '3992', 'name': 'Cliente A', 'canal': 'K+T'},
                '275': {'id': '275', 'name': 'Cliente B', 'canal': 'AUTOSERVICIO'}},
                'rules': {'K+T': 200, 'AUTOSERVICIO': 500},
                'extensions': {'3992:CORE': {'active': True, 'date': '2026-08-14'}}}),
            patch('requests.sessions.Session.request', side_effect=AssertionError('Network in a query')),
        ]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def test_unknown_sku_does_not_reuse_previous(self):
        _, _, ctx = bot.respond('stock 10810 Trelew')
        answer, _, _ = bot.respond('stock 9999999 Trelew', ctx)
        self.assertIn('No encontré el SKU **9999999**', answer)
        self.assertNotIn('Stock actual', answer)

    def test_unknown_name_does_not_reuse_previous(self):
        _, _, ctx = bot.respond('stock 10810 Trelew')
        answer, _, _ = bot.respond('stock producto inexistente Trelew', ctx)
        self.assertIn('No encontré un producto', answer)

    def test_explicit_frescura_overrides_stock(self):
        _, _, ctx = bot.respond('stock 10810 Trelew')
        _, _, ctx = bot.respond('frescura 30789 Madryn', ctx)
        self.assertEqual((ctx['active_topic'], ctx['active_sku'], ctx['active_scope']), ('frescura','30789','MADRYN'))

    def test_scope_followup(self):
        _, _, ctx = bot.respond('stock 10810')
        answer, _, ctx = bot.respond('Madryn', ctx)
        self.assertIn('20,0', answer)
        self.assertEqual(ctx['active_topic'], 'stock')

    def test_pending_scope_can_be_abandoned(self):
        _, _, ctx = bot.respond('stock 10810')
        _, _, ctx = bot.respond('frescura 30789 Madryn', ctx)
        self.assertEqual(ctx['active_topic'], 'frescura')
        self.assertFalse(ctx['pending_stock_scope'])

    def test_both_topes_with_extension(self):
        answer, sources, _ = bot.respond('tope core y value del cliente 3992')
        self.assertIn('CORE: 400', answer)
        self.assertIn('VALUE: 200', answer)
        self.assertIn('14/08/2026', answer)
        self.assertIn('datos/copia al', sources[0])

    def test_change_segment_and_client(self):
        _, _, ctx = bot.respond('tope core 3992')
        answer, _, ctx = bot.respond('y en value?', ctx)
        self.assertIn('VALUE: 200', answer)
        self.assertNotIn('CORE:', answer)
        answer, _, ctx = bot.respond('y el 275?', ctx)
        self.assertIn('VALUE: 500', answer)
        self.assertEqual(ctx['active_client_id'], '275')

    def test_both_segments_after_specific(self):
        _, _, ctx = bot.respond('tope core 3992')
        answer, _, _ = bot.respond('y core y value?', ctx)
        self.assertIn('CORE:', answer); self.assertIn('VALUE:', answer)

    def test_topes_overrides_serial_context(self):
        answer, _, ctx = bot.respond('tope core 3992', {'active_topic': 'edf_location', 'active_serial': '123456'})
        self.assertIn('CORE: 400', answer)
        self.assertEqual(ctx['active_topic'], 'tope')

    def test_explicit_stock_overrides_repago_period_context(self):
        _, _, ctx = bot.respond('stock actual 10810 Trelew', {'active_topic': 'repago', 'active_client_id': '3992'})
        self.assertEqual(ctx['active_topic'], 'stock')

    def test_extension_failure_never_claims_total(self):
        ps._snapshot['extensions_error'] = 'Unavailable'
        answer, _, _ = bot.respond('tope core 3992')
        self.assertIn('200 bultos de tope base', answer)
        self.assertIn('Ampliaciones sin verificar', answer)
        self.assertNotIn('400', answer)

    def test_no_topes_snapshot(self):
        with patch.object(ps, '_load_disk', return_value=None):
            answer, _, _ = bot.respond('tope core 3992')
            self.assertIn('todavía no están disponibles', answer)
            answer, _, _ = bot.respond('stock 10810 Trelew')
            self.assertIn('Stock actual', answer)


class SourceTests(unittest.TestCase):
    def test_rules_fail_closed(self):
        with self.assertRaises(ValueError):
            ps.read_rules('TOPES_CANAL = {}')

    def test_extensions_last_row_wins_and_invalid_date(self):
        frame = pd.DataFrame([
            {'cliente_codigo': '003992', 'accion': 'CORE', 'activa': 'si', 'fecha_extension': '14/08/2026'},
            {'cliente_codigo': '3992', 'accion': 'CORE', 'activa': 'false', 'fecha_extension': '14/08/2026'},
            {'cliente_codigo': '275', 'accion': 'VALUE', 'activa': 'true', 'fecha_extension': ''}])
        parsed = ps.parse_extensions(frame)
        self.assertFalse(parsed['3992:CORE']['active'])
        self.assertFalse(parsed['275:VALUE']['active'])

    def test_channel_priority_matches_planificacion(self):
        self.assertEqual(ps.classify('Autoservicio Kiosco'), 'AUTOSERVICIO')
        self.assertEqual(ps.classify('Mayorista Tradicional'), 'MAYORISTA')
        self.assertEqual(ps.classify('Lista única'), 'K+T')

    def test_health_preserves_data_and_reports_error(self):
        health = describe('Test', {'updated_at': stamp()}, RuntimeError('offline'))
        self.assertTrue(health['available'])
        self.assertIn('Falló', health['warning'])
        self.assertEqual(health['refresh_error'], 'offline')

    def test_health_stale(self):
        self.assertTrue(describe('Test', {'updated_at': '2000-01-01'})['stale'])

    def test_failed_topes_refresh_keeps_previous_snapshot(self):
        previous = {'updated_at': stamp(), 'customers': {'275': {'id': '275'}}}
        with patch.object(ps, '_snapshot', previous), patch.object(ps, '_last_error', None), patch.object(ps.requests, 'get', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                ps.refresh()
            self.assertIs(ps._snapshot, previous)
            self.assertTrue(ps.status()['available'])
            self.assertIsNotNone(ps.status()['refresh_error'])

    def test_initial_start_never_refreshes_synchronously(self):
        with patch.object(auto_refresh, 'start_background_updater') as start, patch.object(auto_refresh, '_cycle', side_effect=AssertionError('blocking')):
            self.assertEqual(auto_refresh.ensure_initial_ready(), {})
            start.assert_called_once()

    def test_sources_refresh_independently(self):
        first_started, second_ran = threading.Event(), threading.Event()
        class Slow:
            @staticmethod
            def refresh(force):
                first_started.set()
                if not second_ran.wait(3):
                    raise AssertionError('sources were refreshed sequentially')
        class Fast:
            @staticmethod
            def refresh(force):
                first_started.wait(3)
                second_ran.set()
        with patch.object(auto_refresh, '_SOURCES', [('slow', Slow), ('fast', Fast)]), patch.object(auto_refresh, '_state', {'running': False}):
            self.assertEqual(auto_refresh._cycle(), {'fast': 'OK', 'slow': 'OK'})


if __name__ == '__main__':
    unittest.main()
