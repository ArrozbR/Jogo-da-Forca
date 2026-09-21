# TESTE DA ETAPA 0 - o IP virtual realmente migra de uma VM para a outra?
#
# Este e o teste que valida (ou derruba) a premissa central do desenho de failover:
# o cliente conecta sempre no mesmo endereco, e nao precisa saber que existe uma
# segunda maquina. Se ele falhar, a descoberta tem de virar lista de enderecos e a
# logica do cliente muda - por isso ele vem antes do codigo de failover.
#
# A sondagem roda intercalada as acoes, no mesmo laco, para medir o tempo real
# em que o servico ficou sem atender.
param(
    [string]$IpVirtual = "192.168.56.10",
    [string]$Vm1 = "192.168.56.11",
    [string]$Vm2 = "192.168.56.12",
    [int]$Porta = 5000,
    [string]$Usuario = "aluno",
    [string]$Senha = "forca2026",
    [string]$Chave = "$env:USERPROFILE\.ssh\forca_vm",
    [string]$Iface = "proj0"
)

$vbm = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
$op = @("-i", $Chave, "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8")

function Remoto($ip, $cmd) { & ssh @op "$Usuario@$ip" $cmd 2>$null }

function Sondar($ip, $porta) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        if (-not ($c.ConnectAsync($ip, $porta).Wait(800) -and $c.Connected)) { return $null }
        $s = $c.GetStream(); $s.ReadTimeout = 800
        $b = [System.Text.Encoding]::UTF8.GetBytes('{"tipo":"ping"}' + "`n")
        $s.Write($b, 0, $b.Length); $s.Flush()
        $buf = New-Object byte[] 4096
        $n = $s.Read($buf, 0, 4096)
        if ($n -le 0) { return $null }
        $linha = ([System.Text.Encoding]::UTF8.GetString($buf, 0, $n) -split "`n")[0]
        return ([regex]::Match($linha, '"servidor":\s*"([^"]+)"')).Groups[1].Value
    } catch { return $null } finally { $c.Dispose() }
}

Write-Host "=== preparacao ===" -ForegroundColor Cyan
# '[s]ervidor.py' e proposital: 'pkill -f servidor.py' casaria com a propria linha de
# comando do shell que o executa e mataria a si mesmo antes do resto rodar - foi
# assim que uma limpeza silenciosamente nao aconteceu e as duas VMs acabaram
# reivindicando o mesmo IP virtual.
foreach ($ip in @($Vm1, $Vm2)) {
    Remoto $ip "pkill -f '[s]ervidor.py' ; echo '$Senha' | sudo -S -p '' ip addr del $IpVirtual/24 dev $Iface 2>/dev/null; true" | Out-Null
}

Write-Host "VM1 assume o IP virtual $IpVirtual"
Remoto $Vm1 "echo '$Senha' | sudo -S -p '' ~/JogoDaForca/infra/assumir_ip.sh $IpVirtual/24 $Iface"

Write-Host "subindo o servidor nas duas VMs"
# O subshell em parenteses e essencial: sem ele o processo em segundo plano herda a
# saida do SSH e a mantem aberta, e o ssh nunca retorna. Redirecionar apenas o
# python nao basta - quem segura o descritor e o shell que ficou em background.
function Subir($ip, $nome) {
    Remoto $ip "cd ~/JogoDaForca && (setsid python3 servidor.py --nome $nome --porta $Porta </dev/null >~/servidor.log 2>&1 &) ; echo iniciado"
}
Subir $Vm1 "VM1" | Out-Null
Subir $Vm2 "VM2" | Out-Null
Start-Sleep -Seconds 2

Write-Host "`n=== sondagem de $IpVirtual`:$Porta ===" -ForegroundColor Cyan
$anterior = "inicio"
$ultimoOk = $null
$quedaEm = $null
$relogio = [System.Diagnostics.Stopwatch]::StartNew()
$resultado = @()

for ($i = 1; $i -le 60; $i++) {

    if ($i -eq 6) {
        Write-Host ">>> DESLIGANDO A VM1 NA FORCA BRUTA (sem FIN, como queda de energia)" -ForegroundColor Yellow
        & $vbm controlvm VM1 poweroff 2>&1 | Out-Null
    }
    if ($i -eq 12) {
        Write-Host ">>> VM2 ASSUMINDO O IP VIRTUAL" -ForegroundColor Yellow
        Remoto $Vm2 "echo '$Senha' | sudo -S -p '' ~/JogoDaForca/infra/assumir_ip.sh $IpVirtual/24 $Iface"
    }

    $quem = Sondar $IpVirtual $Porta
    $rotulo = if ($quem) { $quem } else { "SEM RESPOSTA" }
    $t = $relogio.Elapsed.TotalSeconds

    if ($rotulo -ne $anterior) {
        $extra = ""
        if (-not $quem) { $quedaEm = $t }
        if ($quem -and $quedaEm) { $extra = "  (fora do ar por {0:N1}s)" -f ($t - $quedaEm); $quedaEm = $null }
        Write-Host ("[{0,6:N1}s] {1} -> {2}{3}" -f $t, $anterior, $rotulo, $extra)
        $resultado += "{0:N1}s  {1} -> {2}{3}" -f $t, $anterior, $rotulo, $extra
        $anterior = $rotulo
    }

    if ($i -gt 12 -and $quem -eq "VM2") { Write-Host "`nVM2 assumiu e esta atendendo. Teste concluido." -ForegroundColor Green; break }
    Start-Sleep -Milliseconds 700
}

Write-Host "`n=== RESULTADO ===" -ForegroundColor Cyan
$resultado | ForEach-Object { Write-Host "  $_" }
if ($resultado -match "-> VM2") {
    Write-Host "`nETAPA 0 APROVADA: o IP virtual migrou de VM1 para VM2." -ForegroundColor Green
} else {
    Write-Host "`nETAPA 0 REPROVADA: o IP nao migrou. A descoberta precisa virar lista de enderecos." -ForegroundColor Red
}
