import logging
logger = logging.getLogger("[VictimAgent]")

class VictimAgent:
    def __init__(self, ssh):
        self.ssh = ssh

    def prepare_services(self):
        try:
            logger.info("[Victim] Habilitando password auth e criando usuário tcc...")
            self.ssh.run_command("victim",
                                 "sudo sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && "
                                 "echo 'UseDNS no' | sudo tee -a /etc/ssh/sshd_config >/dev/null && "
                                 "sudo systemctl restart ssh && "
                                 "id -u tcc >/dev/null 2>&1 || (sudo adduser --disabled-password --gecos \"\" tcc && echo \"tcc:123456\" | sudo chpasswd)",
                                 timeout=240
                                 )

            logger.info("[Victim] Subindo HTTP 8080 (python -m http.server)...")
            self.ssh.run_command("victim", "nohup bash -lc 'python3 -m http.server 8080' >/dev/null 2>&1 &", timeout=10)

            self.ensure_http_login_service(port=8081, user="webuser", password="secret")
        except Exception as e:
            logger.error(f"[Victim] Erro preparando serviços: {e}")
            raise

    def ensure_http_login_service(self, port=8081, user="webuser", password="secret"):
        try:
            logger.info(f"[Victim] Garantindo Flask app de login em :{port}...")
            # tenta via apt; se falhar, pip --user
            try:
                self.ssh.run_command("victim",
                                     "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-flask",
                                     timeout=600)
            except Exception as e:
                logger.warning(f"[Victim] python3-flask via apt falhou ({e}); tentando pip.")
                self.ssh.run_command("victim", "python3 -m pip install --user flask", timeout=600)

            script = (
                "bash -lc '\n"
                "sudo install -d -m 755 /opt/tcc\n"
                "sudo tee /opt/tcc/login_app.py >/dev/null <<'PY'\n"
                "from flask import Flask, request, Response\n"
                "import os\n"
                "app = Flask(__name__)\n"
                f"USER = os.environ.get(\"TCC_WEB_USER\",\"{user}\")\n"
                f"PASS = os.environ.get(\"TCC_WEB_PASS\",\"{password}\")\n"
                "@app.post(\"/login\")\n"
                "def login():\n"
                "    u = request.form.get(\"username\",\"\")\n"
                "    p = request.form.get(\"password\",\"\")\n"
                "    if u == USER and p == PASS:\n"
                "        return \"ok\", 200\n"
                "    return Response(\"invalid\", status=401, mimetype=\"text/plain\")\n"
                "@app.get(\"/\")\n"
                "def root():\n"
                "    return \"login-service\", 200\n"
                "if __name__ == \"__main__\":\n"
                f"    app.run(host=\"0.0.0.0\", port={int(port)})\n"
                "PY\n"
                "sudo chmod 644 /opt/tcc/login_app.py\n"
                "'"
            )
            self.ssh.run_command("victim", script, timeout=30)

            run_cmd = (
                "nohup bash -lc '"
                f"TCC_WEB_USER={user} TCC_WEB_PASS={password} python3 /opt/tcc/login_app.py"
                "' >/dev/null 2>&1 &"
            )
            self.ssh.run_command("victim", run_cmd, timeout=10)
            logger.info("[Victim] Login app solicitado — prosseguindo.")
        except Exception as e:
            logger.warning(f"[Victim] Falha ao subir login app: {e}")

    def tail_auth(self, n=20) -> str:
        try:
            return self.ssh.run_command("victim", f"sudo tail -n {n} /var/log/auth.log", timeout=10)
        except Exception as e:
            logger.warning(f"[Victim] Falha ao ler auth.log: {e}")
            return ""
