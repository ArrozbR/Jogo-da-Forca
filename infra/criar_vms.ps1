# Cria as duas VMs do projeto no VirtualBox, em modo bridge.
# Rode de novo com -Recriar para apagar e refazer do zero.
param(
    [string]$Iso = "C:\ISOs\ubuntu-26.04.1-live-server-amd64.iso",
    [string]$Bridge = "Realtek Gaming 2.5GbE Family Controller",
    # 2 GB porque o instalador live do Ubuntu Server roda inteiro na RAM.
    [int]$MemoriaMB = 2048,
    [int]$DiscoMB = 10240,
    [int]$Cpus = 2,
    [switch]$Recriar
)

$ErrorActionPreference = "Stop"
$vbm = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
$base = (& $vbm list systemproperties | Select-String "Default machine folder").ToString().Split(":", 2)[1].Trim()

foreach ($nome in @("VM1", "VM2")) {
    $existe = (& $vbm list vms) -match "`"$nome`""
    if ($existe) {
        if (-not $Recriar) { Write-Host "$nome ja existe, pulando"; continue }
        Write-Host "removendo $nome"
        & $vbm unregistervm $nome --delete
    }

    Write-Host "criando $nome"
    & $vbm createvm --name $nome --ostype Ubuntu_64 --register | Out-Null

    & $vbm modifyvm $nome `
        --memory $MemoriaMB --cpus $Cpus `
        --nic1 bridged --bridgeadapter1 $Bridge `
        --audio-driver none --graphicscontroller vmsvga `
        --boot1 dvd --boot2 disk --boot3 none --boot4 none | Out-Null

    $vdi = Join-Path $base "$nome\$nome.vdi"
    & $vbm createmedium disk --filename $vdi --size $DiscoMB --format VDI | Out-Null

    & $vbm storagectl $nome --name "SATA" --add sata --controller IntelAhci --portcount 2 | Out-Null
    & $vbm storageattach $nome --storagectl "SATA" --port 0 --device 0 --type hdd --medium $vdi | Out-Null

    if (Test-Path $Iso) {
        & $vbm storageattach $nome --storagectl "SATA" --port 1 --device 0 --type dvddrive --medium $Iso | Out-Null
        Write-Host "  ISO anexada"
    } else {
        & $vbm storageattach $nome --storagectl "SATA" --port 1 --device 0 --type dvddrive --medium emptydrive | Out-Null
        Write-Host "  ISO ainda nao existe - anexe depois com storageattach"
    }

    Write-Host "  $nome pronta: ${MemoriaMB}MB, ${Cpus} cpus, disco ${DiscoMB}MB, bridge em '$Bridge'"
}

Write-Host "`n--- VMs registradas ---"
& $vbm list vms
