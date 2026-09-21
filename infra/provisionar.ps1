# Prepara as duas VMs ja instaladas: instala o arping, copia o projeto e
# confere a versao do Python. Idempotente - pode rodar de novo sem estragar.
param(
    [string]$Usuario = "aluno",
    [string]$Senha = "forca2026",
    [string]$Chave = "$env:USERPROFILE\.ssh\forca_vm",
    [string]$Projeto = "G:\Facul\Projetos\SistemasDistribuidos\JogoDaForca"
)

$ErrorActionPreference = "Continue"
$vms = @{ "VM1" = "192.168.56.11"; "VM2" = "192.168.56.12" }
$opcoes = @("-i", $Chave, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")

function Remoto([string]$ip, [string]$comando) {
    & ssh @opcoes "$Usuario@$ip" $comando 2>&1
}

foreach ($nome in $vms.Keys | Sort-Object) {
    $ip = $vms[$nome]
    Write-Host "`n=== $nome ($ip) ===" -ForegroundColor Cyan

    $quem = Remoto $ip "hostname; ip -4 -brief addr show scope global; python3 --version"
    Write-Host ($quem -join "`n")

    Write-Host "-- instalando iputils-arping"
    # sudo -S le a senha da entrada padrao: o usuario do autoinstall exige senha.
    $r = Remoto $ip "echo '$Senha' | sudo -S -p '' apt-get install -y iputils-arping 2>&1 | tail -3"
    Write-Host ($r -join "`n")

    Write-Host "-- copiando o projeto"
    & scp @opcoes -r -q "$Projeto" "$Usuario@${ip}:~/" 2>&1 | ForEach-Object { Write-Host $_ }

    $conf = Remoto $ip "ls ~/JogoDaForca/*.py | wc -l; which arping || echo 'arping AUSENTE'"
    Write-Host ("arquivos .py e arping: " + ($conf -join " | "))

    Remoto $ip "chmod +x ~/JogoDaForca/infra/*.sh" | Out-Null
}

Write-Host "`nProvisionamento concluido nas duas VMs." -ForegroundColor Green
