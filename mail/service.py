# -*- encoding: utf-8 -*-
import logging
import os

from django.conf import settings
from django.core import mail
from django.core.files import File
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.template import Context, Template
from django.utils import timezone
from django.utils.text import slugify

try:
    from django_mailgun import MailgunAPIError
except ImportError:
    class MailgunAPIError(Exception):
        pass

try:
    from djrill import MandrillAPIError
except ImportError:
    class MandrillAPIError(Exception):
        pass

from smtplib import SMTPException

from .models import (
    Attachment,
    Mail,
    MailError,
    MailField,
    MailTemplate,
    Message,
)


logger = logging.getLogger(__name__)


def _can_use_mandrill():
    api_key = None
    result = False
    user_name = None
    if 'DjrillBackend' in settings.EMAIL_BACKEND:
        try:
            api_key = settings.MANDRILL_API_KEY
            user_name = settings.MANDRILL_USER_NAME
            result = api_key and user_name
        except AttributeError:
            pass
    return result


def _check_backends(template_types):
    """Check the backend settings... so we can abort early."""
    if not settings.DEFAULT_FROM_EMAIL:
        raise MailError("No 'DEFAULT_FROM_EMAIL' address in 'settings'.")
    for t in template_types:
        if t == MailTemplate.DJANGO:
            if _can_use_mandrill():
                _using_mandrill()
            else:
                _using_mailgun()
        elif t == MailTemplate.MANDRILL:
            _using_mandrill()


def _get_merge_vars(mail_item):
    result = [ (mf.key, mf.value) for mf in mail_item.mailfield_set.all()]
    return dict(result)


def _mail_process():
    primary_keys = []
    sent = []
    template_types = []
    qs = Mail.objects.filter(
        Q(sent__isnull=True)
        &
        (Q(retry_count__lte=10) | Q(retry_count__isnull=True))
    ).order_by(
        'pk'
    )
    for m in qs:
        primary_keys.append(m.pk)
        if m.message.template:
            template_types.append(m.message.template.template_type)
    _check_backends(set(template_types))
    return _mail_select_and_send(primary_keys)


def _render(text, context):
    t = Template(text)
    c = Context(context)
    return t.render(c)


def _mail_send(m):
    """Send the 'Mail' message."""
    result = None
    try:
        if m.message.template:
            template_type = m.message.template.template_type
        else:
            template_type = None
        if template_type == MailTemplate.DJANGO:
            result = _send_mail_django_template(m)
        elif template_type == MailTemplate.MANDRILL:
            result = _send_mail_mandrill_template(m)
        else:
            result = _send_mail_simple(m)
        m.sent = timezone.now()
        if result:
            m.sent_response_code = result
    except (SMTPException, MailError, MailgunAPIError, MandrillAPIError) as e:
        if hasattr(e, 'message'):
            logger.error(e.message)
        else:
            logger.error(e)
        retry_count = m.retry_count or 0
        m.retry_count = retry_count + 1
    m.save()
    return result


def _mail_select_and_send(primary_keys):
    result = []
    for pk in primary_keys:
        with transaction.atomic():
            try:
                # only send once (if function is called twice at the same time)
                m = Mail.objects.select_for_update(nowait=True).get(pk=pk)
                if not m.sent:
                    if _mail_send(m):
                        result.append(m.pk)
            except DatabaseError:
                # record is locked, so leave alone this time
                pass
    return result


def _send_mail_simple(m):
    """Send message to a single email address using the Django API."""
    if _can_use_mandrill():
        email = mail.EmailMessage(
            m.message.subject,
            m.message.description,
            settings.DEFAULT_FROM_EMAIL,
            [m.email,],
        )
        for attachment in m.message.attachments():
            email.attach_file(attachment.document.file.name)
        email.send(fail_silently=False)
    else:
        mail.send_mail(
            m.message.subject,
            m.message.description,
            settings.DEFAULT_FROM_EMAIL,
            [m.email,],
            fail_silently=False,
        )


def _send_mail_django_template(m):
    merge_vars = _get_merge_vars(m)
    subject, description = _mail_template_render(
        m.message.template.slug,
        merge_vars,
    )
    msg = mail.EmailMultiAlternatives(
        subject=subject,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[m.email],
    )
    if m.message.template.is_html:
        msg.attach_alternative(description, "text/html")
        msg.auto_text = True
    else:
        msg.body = description
        msg.send()


def _send_mail_mandrill_template(m):
    """Send message to a single email address."""
    result = None
    email_addresses = [m.email,]
    msg = mail.EmailMultiAlternatives(
        subject=m.message.subject,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=email_addresses,
    )
    msg.metadata = {
        'user_id': settings.MANDRILL_USER_NAME,
    }
    if m.message.template.is_html:
        msg.attach_alternative(m.message.description, "text/html")
        msg.auto_text = True
    else:
        msg.body = m.message.description
    merge_vars = dict()
    user_vars = _get_merge_vars(m)
    if len(user_vars):
        merge_vars.update ({m.email: user_vars})
    if len(merge_vars):
        msg.merge_vars = merge_vars
    msg.template_name = m.message.template.slug
    # use the subject defined in the mandrill template
    msg.use_template_subject = True
    # use the from address defined in the mandrill template
    msg.use_template_from = True
    msg.send()
    # sending one email, so expecting a single element in this list e.g:
    # [
    #   {
    #     'email': 'testing@pkimber.net',
    #     'status': 'sent',
    #     '_id': '1dffa1548a884dcdb6942f758abff168',
    #     'reject_reason': None
    #   }
    # ]
    for resp in msg.mandrill_response:
        status = resp['status']
        if status in ('sent', 'queued'):
            result = resp['_id']
        else:
            email = ''
            if 'email' in resp:
                email = resp['email']
            if 'reject_reason' in resp:
                reason = resp['reject_reason']
            raise MailError("Failed to send mail '{}' to '{}' [{}] [{}] [{}]".format(
                m.pk, m.email, status, email, reason
            ))
    return result


def _using_mailgun():
    """We are using mailgun (or the server name)... check settings."""
    if not settings.MAILGUN_SERVER_NAME:
        raise MailError("Mailgun server name is not correctly configured")


def _using_mandrill():
    """We are using mandrill... so check settings."""
    if not 'DjrillBackend' in settings.EMAIL_BACKEND:
        raise MailError(
            "The email backend is not 'DjrillBackend'.  You cannot send "
            "messages using Mandrill (or send Mandrill templates)."
        )
    if not _can_use_mandrill():
        raise MailError(
            "The Mandrill user name and/or API key are not configured"
        )


def get_mail_template(slug):
    slug = slugify(slug)
    try:
        return MailTemplate.objects.get(slug=slug)
    except MailTemplate.DoesNotExist:
        raise MailError("Mail template '{}' does not exist.".format(slug))


def init_app_mail():
    pass


def _mail_template_render(template_slug, context):
    description = None
    subject = None
    template = MailTemplate.objects.get(slug=template_slug)
    description = _render(template.description, context)
    subject = _render(template.subject, context)
    return subject, description


@transaction.atomic
def queue_mail_message(
        content_object, email_addresses, subject, description,
        is_html=False, attachments=None):
    """queue a mail message for one or more email addresses.

    The subject and description are fully formed i.e. this function does not
    do any templating.

    """
    if not attachments:
        attachments = []
    if not email_addresses:
        raise MailError(
            "Cannot 'queue_mail_message' without "
            "'email_addresses': '{}'".format(subject)
        )
    message = Message(**dict(
        content_object=content_object,
        subject=subject,
        description=description,
        is_html=is_html,
    ))
    message.save()
    for email in email_addresses:
        mail = Mail(**dict(
            email=email,
            message=message,
        ))
        mail.save()
    for file_name in attachments:
        attachment = Attachment(message=message)
        with open(file_name, 'rb') as f:
            django_file = File(f)
            base_name = os.path.basename(file_name)
            attachment.document.save(base_name, django_file, save=True)
    return message


@transaction.atomic
def queue_mail_template(content_object, template_slug, context):
    """Queue a mail message.  The message will be rendered using the template.

    When the mail is sent, the template will be found and rendered using
    Django or Mandrill.

    The context is a dict containing email addresses and optionally a
    key, value dict for each email address.
    """

    template = get_mail_template(template_slug)
    message = Message(**dict(
        content_object=content_object,
        subject=template.subject,
        template=template,
    ))
    message.save()
    for email in context.keys():
        mail = Mail(**dict(
            email=email,
            message=message,
        ))
        mail.save()
        email_data = context[email]
        if email_data:
            for key in email_data.keys():
                value = email_data.get(key, None)
                if value:
                    mf = MailField(**dict(mail=mail, key=key, value=value))
                    mf.save()
    return message


def send_mail():
    _mail_process()
