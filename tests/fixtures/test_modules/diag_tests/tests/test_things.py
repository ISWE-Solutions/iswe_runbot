from odoo.tests import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestThings(TransactionCase):

    def test_passes(self):
        self.assertEqual(self.env['res.partner'].search_count([('id', '=', -1)]), 0)

    def test_fails(self):
        partner = self.env['res.partner'].create({'name': 'Diag'})
        self.assertEqual(partner.name, 'Something else', "partner name should match")

    def test_errors(self):
        self.env['res.partner'].browse(1).no_such_method()


@tagged('at_install')
class TestAtInstall(TransactionCase):

    def test_at_install_passes(self):
        self.assertTrue(self.env.ref('base.main_company'))


@tagged('post_install', '-at_install')
class TestHttp(HttpCase):

    def test_url(self):
        r = self.url_open('/web/login')
        self.assertEqual(r.status_code, 200)

    def test_tour(self):
        self.start_tour('/odoo', 'no_such_tour', login='admin')
