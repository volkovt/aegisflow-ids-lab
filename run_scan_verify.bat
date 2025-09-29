@echo off
setlocal EnableExtensions DisableDelayedExpansion
title TCC - Scan+Verify (Vagrant/VirtualBox/Zeek)

rem =====================[ CONFIG ]=====================
rem Tempo de captura do tcpdump (segundos). Pode sobrescrever: run_scan_verify 60
set "SCAN_SECONDS=40"
if not "%~1"=="" set "SCAN_SECONDS=%~1"
rem ===================================================

rem Raiz do projeto (pasta que contém "lab")
set "ROOT=%~dp0"
pushd "%ROOT%\lab" || (echo [erro] Nao achei "%ROOT%\lab" & exit /b 1)

echo === TCC: preparando ambiente (Vagrant/VirtualBox) ===
echo [host] vagrant up
vagrant up

echo [host] Aplicando modo promisc (live->controlvm; offline->modifyvm)...
setlocal EnableDelayedExpansion
for %%V in (attacker victim sensor) do (
    set "IDFILE=.vagrant\machines\%%V\virtualbox\id"
    set "VMUUID="
    if exist "!IDFILE!" (
        for /f "usebackq delims=" %%I in ("!IDFILE!") do set "VMUUID=%%I"
        if defined VMUUID (
            echo [host] VM %%V UUID=!VMUUID!
            VBoxManage controlvm !VMUUID! nicpromisc2 allow-all >nul 2>&1
            if errorlevel 1 VBoxManage modifyvm !VMUUID! --nicpromisc2 allow-all >nul 2>&1
        ) else (
            echo [warn] Nao consegui ler UUID de %%V a partir de !IDFILE!
        )
    ) else (
        echo [warn] UUID de %%V nao encontrado em !IDFILE!
    )
)
endlocal

echo.
echo === Iniciando Zeek no sensor e preparando captura (%SCAN_SECONDS%s) ===
rem 1) Preparar diretorios e matar restos antigos
vagrant ssh sensor -c "sudo bash -lc 'mkdir -p /var/log/pcap /var/log/zeek /var/run && chmod 0755 /var/log/pcap /var/log/zeek'"
vagrant ssh sensor -c "sudo bash -lc 'pkill -x zeek 2>/dev/null || true; pkill -x tcpdump 2>/dev/null || true'"

rem 2) Limpar pcap antigo (garante timestamp fresco)
vagrant ssh sensor -c "sudo bash -lc 'rm -f /tmp/ssh.pcap || true'"

rem 3) Criar/zerar zeek.out e iniciar Zeek (robusto
vagrant ssh sensor -c "sudo bash -lc 'mkdir -p /var/log/zeek && chmod 0755 /var/log/zeek && : > /var/log/zeek/zeek.out; ZEEXE=$(command -v zeek 2>/dev/null || echo /opt/zeek/bin/zeek); if [ ! -x $ZEEXE ]; then echo [erro] zeek nao encontrado em PATH nem em /opt/zeek/bin/zeek >> /var/log/zeek/zeek.out; else nohup $ZEEXE -C -i eth1 local >> /var/log/zeek/zeek.out 2>&1 & fi; sleep 1; tail -n 3 /var/log/zeek/zeek.out 2>/dev/null || true'"

rem 4) Iniciar captura tcpdump por N segundos na porta 22/tcp
vagrant ssh sensor -c "sudo bash -lc 'nohup timeout %SCAN_SECONDS% tcpdump -ni eth1 port 22 -w /tmp/ssh.pcap > /var/log/pcap/tcpdump22.out 2>&1 & sleep 1; tail -n 2 /var/log/pcap/tcpdump22.out 2>/dev/null || true'"

echo.
echo === Disparando trafego SSH no atacante (gera handshake p/ ssh.log) ===
vagrant ssh attacker -c "bash -lc 'touch ~/.hushlogin || true'"
vagrant ssh attacker -c "bash -lc 'nmap -sV -Pn -p22 192.168.56.12'"
rem Handshake SSH sem autenticar (fecha logo apos o banner)
vagrant ssh attacker -c "bash -lc 'ssh -o PreferredAuthentications=none -o PubkeyAuthentication=no -o PasswordAuthentication=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 192.168.56.12 exit || true'"

echo.
echo === Aguardando flush dos logs do Zeek ===
vagrant ssh sensor -c "bash -lc 'sleep 5'"

echo.
echo === Validacao: pcap e logs do Zeek ===
vagrant ssh sensor -c "bash -lc 'ls -lh /tmp/ssh.pcap || true; echo; echo CONN_LOG_LAST_40; tail -n 40 /var/log/zeek/conn.log 2>/dev/null || echo NO_CONN_LOG; echo; echo SSH_LOG_LAST_20; tail -n 20 /var/log/zeek/ssh.log 2>/dev/null || echo NO_SSH_LOG; echo; echo ZEEK_OUT_LAST_20; tail -n 20 /var/log/zeek/zeek.out 2>/dev/null || true; echo; echo TCPDUMP_OUT_LAST_10; tail -n 10 /var/log/pcap/tcpdump22.out 2>/dev/null || true'"

echo.
echo === (Opcional) Rodar ETL do TCC apontando para /var/log/zeek ===
echo Remova o "rem" abaixo se quiser disparar seu ETL agora.
rem python -m etl_netsec run data\EXP_SCAN_BRUTE

echo.
echo === FIM ===
popd
exit /b 0
