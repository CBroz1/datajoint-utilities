import requests
import json

from . import Notifier


class MailgunEmailNotifier(Notifier):
    def __init__(
        self,
        mailgun_api_key,
        mailgun_domain_name,
        sender_name,
        sender_email,
        receiver_emails,
    ):
        self.auth = ("api", mailgun_api_key)
        self.request_url = f"https://api.mailgun.net/v3/{mailgun_domain_name}/messages"
        self.body = {
            "from": f"{sender_name} <{sender_email}>",
            "to": sender_email,
            "bcc": receiver_emails,
        }

    def notify(self, title, message, **kwargs):
        body = {**self.body, "subject": title, "text": message}
        requests.post(self.request_url, auth=self.auth, data=body)


class HubSpotTemplateEmailNotifier(Notifier):
    def __init__(
        self,
        hubspot_api_key,
        email_template_id,
        sender_name,
        sender_email,
        receiver_emails,
    ):
        self.request_url = (
            "https://api.hubapi.com/marketing/v3/transactional/single-email/send"
        )
        self.headers = {
            "Authorization": f"Bearer {hubspot_api_key}",
            "Content-Type": "application/json",
        }
        self.body = {
            "emailId": email_template_id,
            "message": {
                "from": f"{sender_name} <{sender_email}>",
                "to": receiver_emails[0],
                "bcc": receiver_emails,
            },
        }

    def notify(self, title, message, **kwargs):
        body = {**self.body, "customProperties": {**kwargs, "status_message": message}}
        requests.post(
            self.request_url, headers=self.headers, data=json.dumps(body, default=str)
        )
