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
                "id -u tcc || (sudo adduser --disabled-password --gecos \"\" tcc && echo \"tcc:123456\" | sudo chpasswd)",
                timeout=180
            )

            logger.info("[Victim] Subindo HTTP 8080 (python -m http.server)...")
            self.ssh.run_command("victim", "nohup python3 -m http.server 8080 >/dev/null 2>&1 &", timeout=10)

            # NOVO: serviço de login HTTP para BRUTE-HTTP (porta 8081)
            self.ensure_http_login_service(port=8081, user="webuser", password="secret")

        except Exception as e:
            logger.error(f"[Victim] Erro preparando serviços: {e}")
            raise

    def ensure_http_login_service(self, port=8081, user="webuser", password="secret"):
        """
        Sobe um micro-serviço Flask em :8081 com /login.
        Responde 200 OK com 'ok' quando credencial bate; senão 401 e 'invalid'.
        """
        try:
            logger.info(f"[Victim] Garantindo Flask app de login em :{port}...")
            # tenta instalar via apt; se falhar, usa pip
            try:
                self.ssh.run_command("victim", "sudo apt-get update -y && sudo apt-get install -y python3-flask", timeout=300)
            except Exception as e:
                logger.warning(f"[Victim] python3-flask via apt falhou ({e}); tentando pip.")
                self.ssh.run_command("victim", "python3 -m pip install --user flask", timeout=300)

            cmd_write = r"""bash -lc 'sudo mkdir -p /opt/tcc && cat > /opt/tcc/login_app.py <<PY
                from flask import Flask, request, Response
                import os
                
                app = Flask(__name__)
                USER = os.environ.get("TCC_WEB_USER","webuser")
                PASS = os.environ.get("TCC_WEB_PASS","secret")
                
                @app.post("/login")
                def login():
                    u = request.form.get("username","")
                    p = request.form.get("password","")
                    if u == USER and p == PASS:
                        return "ok", 200
                    return Response("invalid", status=401, mimetype="text/plain")
                
                @app.get("/")
                def root():
                    return "login-service", 200
                
                if __name__ == "__main__":
                    app.run(host="0.0.0.0", port=%d)
                PY
                '""" % int(port)
            self.ssh.run_command("victim", cmd_write, timeout=10)

            run_cmd = (
                f"nohup bash -lc 'TCC_WEB_USER={user} TCC_WEB_PASS={password} "
                f"python3 /opt/tcc/login_app.py' >/dev/null 2>&1 &"
            )
            self.ssh.run_command("victim", run_cmd, timeout=8)

            # healthcheck simples
            logger.info("[Victim] Login app solicitado — prosseguindo.")
        except Exception as e:
            logger.warning(f"[Victim] Falha ao subir login app: {e}")

    def tail_auth(self, n=20) -> str:
        try:
            return self.ssh.run_command("victim", f"sudo tail -n {n} /var/log/auth.log", timeout=10)
        except Exception as e:
            logger.warning(f"[Victim] Falha ao ler auth.log: {e}")
            return ""
