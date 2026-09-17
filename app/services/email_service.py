import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
import base64
import logging
import httpx

logger = logging.getLogger(__name__)

class EmailService:
    def __init__(self):
        self.smtp_host = os.getenv('EMAIL_HOST')
        self.smtp_port = int(os.getenv('EMAIL_PORT', 587))
        self.smtp_user = os.getenv('EMAIL_HOST_USER')
        self.smtp_password = os.getenv('EMAIL_HOST_PASSWORD')
        self.from_email = os.getenv('EMAIL_FROM')
        self.use_tls = os.getenv('EMAIL_USE_TLS', 'True').lower() == 'true'
        self.use_ssl = os.getenv('EMAIL_USE_SSL', 'False').lower() == 'true'

    @classmethod
    def from_project_config(cls, db, project_id: int) -> "EmailService":
        """Create EmailService from project's CustomerSMTPConfig, falling back to env vars."""
        from app.models import CustomerSMTPConfig

        smtp_cfg = db.query(CustomerSMTPConfig).filter(
            CustomerSMTPConfig.project_id == project_id,
            CustomerSMTPConfig.is_active == True,
        ).first()

        instance = cls()  # loads env vars as defaults

        if smtp_cfg:
            cls._apply_smtp_config(instance, smtp_cfg)

        return instance

    @classmethod
    def from_smtp_config_id(cls, db, config_id: int, project_id: int) -> "EmailService":
        """Create EmailService from a specific CustomerSMTPConfig by ID (with project_id guard)."""
        from app.models import CustomerSMTPConfig

        smtp_cfg = db.query(CustomerSMTPConfig).filter(
            CustomerSMTPConfig.id == config_id,
            CustomerSMTPConfig.project_id == project_id,
        ).first()

        if not smtp_cfg:
            raise ValueError(f"SMTP config {config_id} not found for project {project_id}")

        instance = cls()
        cls._apply_smtp_config(instance, smtp_cfg)
        return instance

    @staticmethod
    def _apply_smtp_config(instance: "EmailService", smtp_cfg) -> None:
        """Apply a CustomerSMTPConfig to an EmailService instance."""
        from cryptography.fernet import Fernet

        enc_key = os.getenv("ENCRYPTION_KEY")
        password = smtp_cfg.smtp_password
        if enc_key and password:
            try:
                f = Fernet(enc_key.encode())
                password = f.decrypt(password.encode()).decode()
            except Exception:
                pass

        instance.smtp_host = smtp_cfg.smtp_server
        instance.smtp_port = smtp_cfg.smtp_port
        instance.smtp_user = smtp_cfg.smtp_username
        instance.smtp_password = password
        instance.use_tls = smtp_cfg.smtp_use_tls
        instance.use_ssl = smtp_cfg.smtp_use_ssl
        instance.from_email = smtp_cfg.from_email

    def send_whatsapp_qr_code(self, to_email: str, qr_code_base64: str, user_name: str = None) -> dict:
        """
        Send WhatsApp QR code via email

        Args:
            to_email: Recipient email address
            qr_code_base64: Base64 encoded QR code image (data:image/png;base64,...)
            user_name: Name of the user (optional)

        Returns:
            dict: Success status and message
        """
        try:
            # Remove the data URL prefix if present
            if qr_code_base64.startswith('data:image/png;base64,'):
                qr_code_base64 = qr_code_base64.replace('data:image/png;base64,', '')

            # Decode base64 to bytes
            qr_code_bytes = base64.b64decode(qr_code_base64)

            # Create message
            msg = MIMEMultipart('related')
            msg['Subject'] = 'Conecte seu WhatsApp - Código QR'
            msg['From'] = self.from_email
            msg['To'] = to_email

            # Create HTML body
            greeting = f"Olá {user_name}," if user_name else "Olá,"

            html_body = f"""
            <html>
                <head>
                    <style>
                        body {{
                            font-family: Arial, sans-serif;
                            line-height: 1.6;
                            color: #333;
                        }}
                        .container {{
                            max-width: 600px;
                            margin: 0 auto;
                            padding: 20px;
                        }}
                        .header {{
                            background-color: #25D366;
                            color: white;
                            padding: 20px;
                            text-align: center;
                            border-radius: 8px 8px 0 0;
                        }}
                        .content {{
                            background-color: #f9f9f9;
                            padding: 30px;
                            border-radius: 0 0 8px 8px;
                        }}
                        .qr-container {{
                            text-align: center;
                            margin: 20px 0;
                            padding: 20px;
                            background-color: white;
                            border-radius: 8px;
                            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                        }}
                        .qr-code {{
                            max-width: 300px;
                            height: auto;
                            border: 3px solid #25D366;
                            border-radius: 8px;
                            padding: 10px;
                            background-color: white;
                        }}
                        .instructions {{
                            background-color: #e8f5e9;
                            padding: 15px;
                            border-left: 4px solid #25D366;
                            margin: 20px 0;
                        }}
                        .instructions h3 {{
                            margin-top: 0;
                            color: #25D366;
                        }}
                        .instructions ol {{
                            margin: 10px 0;
                            padding-left: 20px;
                        }}
                        .instructions li {{
                            margin: 8px 0;
                        }}
                        .footer {{
                            text-align: center;
                            margin-top: 20px;
                            padding-top: 20px;
                            border-top: 1px solid #ddd;
                            font-size: 12px;
                            color: #666;
                        }}
                    </style>
                </head>
                <body>
                    <div class="container">
                        <div class="header">
                            <h1 style="margin: 0;">📱 Conecte seu WhatsApp</h1>
                        </div>
                        <div class="content">
                            <p>{greeting}</p>

                            <p>Para conectar seu WhatsApp, escaneie o código QR abaixo:</p>

                            <div class="qr-container">
                                <h2 style="color: #25D366; margin-top: 0;">Capture aqui:</h2>
                                <img src="cid:qrcode" class="qr-code" alt="WhatsApp QR Code">
                            </div>

                            <div class="instructions">
                                <h3>📋 Como escanear:</h3>
                                <ol>
                                    <li><strong>Abra o WhatsApp</strong> no seu celular</li>
                                    <li>Vá em <strong>Menu → Aparelhos Conectados</strong></li>
                                    <li>Toque em <strong>"Conectar Aparelho"</strong></li>
                                    <li><strong>Escaneie este código QR</strong></li>
                                </ol>
                            </div>

                            <p style="color: #666; font-size: 14px;">
                                ⚠️ <strong>Importante:</strong> Este código QR é válido por tempo limitado.
                                Se expirar, você pode gerar um novo no painel.
                            </p>
                        </div>
                        <div class="footer">
                            <p>Este é um email automático. Por favor, não responda.</p>
                        </div>
                    </div>
                </body>
            </html>
            """

            # Attach HTML body
            msg_html = MIMEText(html_body, 'html')
            msg.attach(msg_html)

            # Attach QR code image
            msg_image = MIMEImage(qr_code_bytes)
            msg_image.add_header('Content-ID', '<qrcode>')
            msg.attach(msg_image)

            # Send email
            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                if self.use_tls:
                    server.starttls()

            server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)
            server.quit()

            logger.info(f"WhatsApp QR code email sent successfully to {to_email}")
            return {
                "success": True,
                "message": f"Email enviado com sucesso para {to_email}"
            }

        except Exception as e:
            logger.error(f"Error sending WhatsApp QR code email: {e}")
            return {
                "success": False,
                "message": f"Erro ao enviar email: {str(e)}"
            }

    def send_html_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        from_email: str = None,
        from_name: str = None,
        reply_to: str = None,
        attachments: list = None,
        message_id: str = None,
        in_reply_to: str = None,
        references: str = None,
        body_format: str = None,
        extra_headers: dict = None,
    ) -> dict:
        """
        Send an HTML email via SMTP.

        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML body content
            from_email: Optional sender email override
            from_name: Optional sender name override
            reply_to: Optional reply-to address
            attachments: Optional list of dicts with 'url' and optional 'filename'

        Returns:
            dict: Success status and message
        """
        submission_started = False
        try:
            if not self.smtp_host or not self.smtp_user:
                return {"success": False, "message": "SMTP not configured"}

            import re

            # If body_format is "plain", convert \n to <br> for the HTML part
            if body_format == "plain" or (
                body_format is None and not re.search(r'<[a-z][\s\S]*>', html_body, re.IGNORECASE)
            ):
                html_body = html_body.replace('\n', '<br>\n')

            if attachments:
                # Mixed container for text + attachments
                msg = MIMEMultipart('mixed')
                alt_part = MIMEMultipart('alternative')
                plain_text = re.sub(r'<[^>]+>', '', html_body)
                alt_part.attach(MIMEText(plain_text, 'plain'))
                alt_part.attach(MIMEText(html_body, 'html'))
                msg.attach(alt_part)

                # Load and attach each file (local media-asset path or HTTP URL)
                from app.services.email_attachments import load_attachment_bytes
                for att in attachments:
                    loaded = load_attachment_bytes(att)
                    if not loaded:
                        continue
                    att_bytes, att_filename = loaded
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(att_bytes)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{att_filename}"')
                    msg.attach(part)
            else:
                msg = MIMEMultipart('alternative')
                plain_text = re.sub(r'<[^>]+>', '', html_body)
                msg.attach(MIMEText(plain_text, 'plain'))
                msg.attach(MIMEText(html_body, 'html'))

            msg['Subject'] = subject
            sender_email = from_email or self.from_email
            if from_name:
                msg['From'] = f"{from_name} <{sender_email}>"
            else:
                msg['From'] = sender_email
            msg['To'] = to_email
            if reply_to:
                msg['Reply-To'] = reply_to
            if message_id:
                msg['Message-ID'] = message_id
            if in_reply_to:
                msg['In-Reply-To'] = in_reply_to
            if references:
                msg['References'] = references

            # Extra headers (List-Unsubscribe, etc.)
            if extra_headers:
                for hdr_name, hdr_value in extra_headers.items():
                    msg[hdr_name] = hdr_value

            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                if self.use_tls:
                    server.starttls()

            server.login(self.smtp_user, self.smtp_password)
            submission_started = True
            server.send_message(msg)
            try:
                server.quit()
            except Exception as exc:
                logger.warning("SMTP quit failed after accepted message %s: %s", message_id, exc)

            logger.info(f"HTML email sent successfully to {to_email}")
            return {
                "success": True,
                "message": f"Email sent successfully to {to_email}",
                "provider_message_id": message_id,
            }

        except Exception as e:
            logger.error(f"Error sending HTML email: {e}")
            ambiguous = submission_started and isinstance(
                e,
                (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionError, OSError),
            )
            return {
                "success": False,
                "message": f"Failed to send email: {str(e)}",
                "provider_message_id": message_id,
                "error_code": "submission_unknown" if ambiguous else "smtp_rejected",
            }

email_service = EmailService()
