from odoo import fields, models

class M(models.Model):
    _name = "diag.thing"
    _description = "x"
    partner_id = fields.Many2one("res.no_such_model")
