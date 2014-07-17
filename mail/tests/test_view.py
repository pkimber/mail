# -*- encoding: utf-8 -*-
from __future__ import unicode_literals

from django.core.urlresolvers import reverse
from django.test import TestCase

from login.tests.factories import TEST_PASSWORD
from login.tests.scenario import (
    default_scenario_login,
    get_user_staff,
)

from mail.models import (
    MailTemplate,
    TEMPLATE_TYPE_DJANGO,
)
from mail.service import init_mail_template


class TestView(TestCase):

    def setUp(self):
        default_scenario_login()
        staff = get_user_staff()
        self.assertTrue(
            self.client.login(username=staff.username, password=TEST_PASSWORD)
        )

    def test_template_update(self):
        template = init_mail_template(
            'hello',
            'Welcome...',
            '',
            False,
            TEMPLATE_TYPE_DJANGO
        )
        url = reverse('mail.template.update', kwargs=dict(slug=template.slug))
        response = self.client.post(
            url,
            dict(
                subject='123',
                description='ABC',
            )
        )
        self.assertEqual(response.status_code, 302)
        template = MailTemplate.objects.get(slug='hello')
        self.assertEqual('ABC', template.description)
