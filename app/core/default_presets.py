def preset_all() -> str:
    return (
        "exp_id: \"EXP_ALL\"\n"
        "targets:\n"
        "  victim_ip: \"192.168.56.20\"\n"
        "capture:\n"
        "  rotate_seconds: 300\n"
        "  rotate_size_mb: 100\n"
        "  zeek_rotate_seconds: 3600\n"
        "actions:\n"
        "  - name: \"nmap_scan\"\n"
        "    params:\n"
        "      output: \"exp_scan.nmap\"\n"
        "  - name: \"hydra_brute\"\n"
        "    params:\n"
        "      user: \"tcc\"\n"
        "      pass_list: [\"123456\", \"wrongpass\"]\n"
        "      output: \"exp_brute.hydra\"\n"
        "  - name: \"slowhttp_dos\"\n"
        "    params:\n"
        "      port: 8080\n"
        "      duration_s: 120\n"
        "      concurrency: 400\n"
        "      rate: 150\n"
        "      output_prefix: \"exp_dos\"\n"
    )


def preset_scan_brute() -> str:
    return (
        "exp_id: \"EXP_SCAN_BRUTE\"\n"
        "targets:\n"
        "  victim_ip: \"192.168.56.20\"\n"
        "capture:\n"
        "  rotate_seconds: 300\n"
        "  rotate_size_mb: 100\n"
        "  zeek_rotate_seconds: 3600\n"
        "actions:\n"
        "  - name: \"nmap_scan\"\n"
        "    params:\n"
        "      output: \"exp_scan.nmap\"\n"
        "  - name: \"hydra_brute\"\n"
        "    params:\n"
        "      user: \"tcc\"\n"
        "      pass_list: [\"123456\", \"wrongpass\"]\n"
        "      output: \"exp_brute.hydra\"\n"
    )


def preset_dos() -> str:
    return (
        "exp_id: \"EXP_DOS\"\n"
        "targets:\n"
        "  victim_ip: \"192.168.56.20\"\n"
        "capture:\n"
        "  rotate_seconds: 300\n"
        "  rotate_size_mb: 100\n"
        "  zeek_rotate_seconds: 3600\n"
        "actions:\n"
        "  - name: \"slowhttp_dos\"\n"
        "    params:\n"
        "      port: 8080\n"
        "      duration_s: 180\n"
        "      concurrency: 600\n"
        "      rate: 200\n"
        "      output_prefix: \"exp_dos\"\n"
    )

def preset_brute_http() -> str:
    return (
        "exp_id: \"EXP_BRUTE_HTTP\"\n"
        "targets:\n"
        "  victim_ip: \"192.168.56.20\"\n"
        "capture:\n"
        "  rotate_seconds: 300\n"
        "  rotate_size_mb: 100\n"
        "  zeek_rotate_seconds: 3600\n"
        "actions:\n"
        "  - name: \"hydra_http_post\"\n"
        "    params:\n"
        "      path: \"/login\"\n"
        "      user: \"webuser\"\n"
        "      pass_list: [\"secret\", \"123456\", \"admin\", \"password\"]\n"
        "      fail_regex: \"invalid\"\n"
        "      port: 8081\n"
        "      output: \"exp_brute_http.hydra\"\n"
        "      threads: 4\n"
    )

def preset_heavy_syn() -> str:
    return (
        "exp_id: \"EXP_HEAVY_SYN\"\n"
        "targets:\n"
        "  victim_ip: \"192.168.56.20\"\n"
        "capture:\n"
        "  rotate_seconds: 120\n"
        "  rotate_size_mb: 200\n"
        "  zeek_rotate_seconds: 1200\n"
        "actions:\n"
        "  - name: \"hping3_syn\"\n"
        "    params:\n"
        "      dst_port: 8080\n"
        "      rate_pps: 800\n"
        "      duration_s: 120\n"
    )