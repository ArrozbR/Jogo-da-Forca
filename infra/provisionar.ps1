# Prepara as VMs ja instaladas: instala o arping, copia o projeto e confere a
# versao do Python. Idempotente - pode rodar de novo sem estragar.
#
# Com uma VM por notebook, cada um provisiona so a sua:
#   infra\provisionar.ps1 -Vms VM1      (no notebook A)
#   infra\provisionar.ps1 -Vms VM2      (no notebook B)
param(
    [string[]]$Vms = @("VM1", "VM2"),
    [string]$Usuario = "aluno",
    [string]$Senha = "forca2026",
    [string]$Chave = "$env:USERPROFILE\.ssh\forca_vm",
    [string]$Projeto = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Continue"
$enderecos = @{ "VM1" = "192.168.56.11"; "VM2" = "192.168.56.12" }
# $alvos, e nao $vms: no PowerShell nome de variavel nao diferencia maiusculas,
# entao $vms E o parametro $Vms. A versao anterior criava a tabela com esse nome,
# apagava a lista pedida, nao copiava nada e ainda dizia "concluido".
$alvos = @{}
foreach ($v in $Vms) { $alvos[$v] = $enderecos[$v] }
$opcoes = @("-i", $Chave, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")

function Remoto([string]$ip, [string]$comando) {
    & ssh @opcoes "$Usuario@$ip" $comando 2>&1
}

foreach ($nome in $alvos.Keys | Sort-Object) {
    $ip = $alvos[$nome]
    Write-Host "`n=== $nome ($ip) ===" -ForegroundColor Cyan

    $quem = Remoto $ip "hostname; ip -4 -brief addr show scope global; python3 --version"
    Write-Host ($quem -join "`n")

    Write-Host "-- instalando iputils-arping"
    # sudo -S le a senha da entrada padrao: o usuario do autoinstall exige senha.
    $r = Remoto $ip "echo '$Senha' | sudo -S -p '' apt-get install -y iputils-arping 2>&1 | tail -3"
    Write-Host ($r -join "`n")

    Write-Host "-- copiando o projeto"
    # Só o que a VM precisa para rodar. Copiar a pasta inteira arrastaria o .git,
    # cujos objetos sao gravados somente-leitura: recopiar por cima falha com
    # "permission denied" e enche a tela de erro sem nada de errado acontecer.
    Remoto $ip "mkdir -p ~/JogoDaForca/infra" | Out-Null
    & scp @opcoes -q "$Projeto\*.py" "$Projeto\palavras.txt" "$Usuario@${ip}:~/JogoDaForca/" 2>&1 |
        ForEach-Object { Write-Host $_ }
    & scp @opcoes -q "$Projeto\infra\*.sh" "$Projeto\infra\*.py" "$Usuario@${ip}:~/JogoDaForca/infra/" 2>&1 |
        ForEach-Object { Write-Host $_ }

    $conf = Remoto $ip "ls ~/JogoDaForca/*.py | wc -l; which arping || echo 'arping AUSENTE'"
    Write-Host ("arquivos .py e arping: " + ($conf -join " | "))

    Remoto $ip "chmod +x ~/JogoDaForca/infra/*.sh" | Out-Null
}

Write-Host "`nProvisionamento concluido: $(($alvos.Keys | Sort-Object) -join ', ')." -ForegroundColor Green
