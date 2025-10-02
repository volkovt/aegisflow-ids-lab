@echo off
chcp 65001 >NUL
setlocal

echo ===========================================================
echo TCC - RUN SCAN VERIFY (clean zeek) - %DATE% %TIME%
echo ===========================================================
echo.

REM --- 1) Up das VMs (mantem comportamento atual do seu run)
echo [1/6] Subindo/checando Vagrant (attacker, victim, sensor)...
REM vagrant up

echo.

REM --- 2) Limpar Zeek/tcpdump no sensor e iniciar captura limpa
echo [2/6] Limpeza de Zeek e (re)inicio de tcpdump + zeek no sensor...
type sensor_init.sh | vagrant ssh sensor -c "sudo bash -s"

echo.

REM --- 3) Validar que sensor está escutando (tcpdump output quick)
echo [3/6] Verificando tcpdump (status) no sensor...
vagrant ssh sensor -c "sudo bash -lc 'echo \"--- tcpdump status (ultima saida) ---\"; tail -n 20 /var/log/pcap/tcpdump.out 2>/dev/null || true; echo \"--- zeek.out tail ---\"; tail -n 40 /var/log/zeek/zeek.out 2>/dev/null || true'"

echo.

REM --- 4) Gerar tráfego: brute manual no attacker (gera hydra_manual.log)
echo [4/6] Disparando brute curto no attacker (hydra)...
type attacker_brute.sh | vagrant ssh attacker -c "bash -s"

echo.

REM --- 5) Sonda rápida no sensor para ver pacotes chegando (6-8s)
echo [5/6] Sondando pacotes SSH no sensor (8s)...
type sensor_probe.sh | vagrant ssh sensor -c "sudo bash -s"

echo.

REM --- 6) Coletar saídas e salvar resumo local
echo [6/6] Coletando resumos dos logs (conn.log, ssh.log, pcap tamanho)...
type sensor_collect.sh | vagrant ssh sensor -c "sudo bash -s"

endlocal