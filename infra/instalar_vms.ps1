# Instala o Ubuntu Server nas duas VMs de forma desassistida.
#
# Rede host-only, e nao bridge. Motivo: o FortiClient injeta uma rota de metrica 0
# para a rede local e sequestra o trafego para dentro do tunel, tornando as VMs
# inalcancaveis. Host-only tambem torna o projeto independente da rede onde ele
# roda - essencial, porque na apresentacao a rede sera outra.
#
# Cada VM tem duas placas: host-only para o projeto (IP fixo) e NAT para internet
# (host-only nao roteia, e o apt precisa sair).
param(
    [string]$Iso = "C:\ISOs\ubuntu-26.04.1-live-server-amd64.iso",
    [string]$Usuario = "aluno",
    [string]$Senha = "forca2026",
    [string]$ChavePublica = "$env:USERPROFILE\.ssh\forca_vm.pub",
    [string[]]$Vms = @("VM1", "VM2")
)

$ErrorActionPreference = "Stop"
$vbm = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
$hostonly = "VirtualBox Host-Only Ethernet Adapter"
$pub = (Get-Content $ChavePublica -Raw).Trim()

$conf = @{
    "VM1" = @{ ip = "192.168.56.11"; mac1 = "080027A00011"; mac2 = "080027A00012" }
    "VM2" = @{ ip = "192.168.56.12"; mac1 = "080027A00021"; mac2 = "080027A00022" }
}

function MacComDoisPontos($m) { ($m -split '(.{2})' | Where-Object { $_ }) -join ':' }

$modelo = @'
#cloud-config
autoinstall:
  version: 1
  apt:
    fallback: offline-install
  locale: @@VBOX_INSERT_LOCALE@@
  keyboard:
    layout: br
  shutdown: reboot
  storage:
    layout:
      name: direct
    swap:
      size: 0
  identity:
    hostname: '@@VBOX_INSERT_HOSTNAME_WITHOUT_DOMAIN@@'
    username: '@@VBOX_INSERT_USER_LOGIN@@'
    realname: '@@VBOX_INSERT_USER_FULL_NAME@@'
    password: '@@VBOX_INSERT_USER_PASSWORD_SHACRYPT512@@'
  ssh:
    install-server: true
    allow-pw: true
    authorized-keys:
      - '__CHAVE__'
  packages:
    - iputils-arping
  network:
    version: 2
    ethernets:
      projeto:
        match:
          macaddress: '__MAC1__'
        set-name: proj0
        dhcp4: false
        addresses:
          - __IP__/24
      internet:
        match:
          macaddress: '__MAC2__'
        set-name: net0
        dhcp4: true
  user-data:
    timezone: @@VBOX_INSERT_TIME_ZONE_UX@@
  late-commands:
    - cp /cdrom/vboxpostinstall.sh /target/root/vboxpostinstall.sh
    - chmod +x /target/root/vboxpostinstall.sh
    - curtin in-target --target=/target -- /bin/bash /root/vboxpostinstall.sh --direct
'@

foreach ($nome in $Vms) {
    $c = $conf[$nome]

    Write-Host "configurando placas de $nome"
    # 1 nucleo e obrigatorio: com o VBS do Windows ativo o VirtualBox roda sobre o
    # backend do Hyper-V, que travava o guest em laco a 100% de CPU com 2 vCPUs.
    # O servidor e um event loop de thread unica, entao nao perde nada.
    # 2048 MB apenas para o instalador; reduza para 1024 depois de instalar.
    & $vbm modifyvm $nome `
        --nic1 hostonly --hostonlyadapter1 $hostonly --macaddress1 $c.mac1 `
        --nic2 nat --macaddress2 $c.mac2 `
        --cpus 1 --paravirtprovider kvm --memory 2048 | Out-Null

    $texto = $modelo.Replace("__CHAVE__", $pub).
                     Replace("__IP__", $c.ip).
                     Replace("__MAC1__", (MacComDoisPontos $c.mac1).ToLower()).
                     Replace("__MAC2__", (MacComDoisPontos $c.mac2).ToLower())

    $tpl = "C:\ISOs\autoinstall_$nome.yaml"
    [System.IO.File]::WriteAllText($tpl, $texto.Replace("`r`n", "`n"))

    $aux = "C:\ISOs\aux-$nome-$(Get-Date -Format HHmmss)"
    New-Item -ItemType Directory -Path $aux -Force | Out-Null

    & $vbm storagectl $nome --name "SATA" --portcount 4 | Out-Null

    Write-Host "preparando instalacao de $nome (host-only $($c.ip))"
    # Sem --start-vm de proposito: o 'unattended install' deixa a ordem de boot com
    # disk na frente, e uma VM que ja tem sistema instalado ignora o instalador e
    # boota o sistema velho. Preparamos, corrigimos a ordem, e so entao ligamos.
    & $vbm unattended install $nome `
        --iso=$Iso `
        --user=$Usuario --user-password=$Senha --full-user-name="Aluno" `
        --hostname="$($nome.ToLower()).local" `
        --locale=pt_BR --country=BR --time-zone=America/Sao_Paulo `
        --script-template=$tpl --auxiliary-base-path="$aux\" `
        --no-install-additions 2>&1 | Out-Null

    & $vbm modifyvm $nome --boot1 dvd --boot2 disk --boot3 none --boot4 none | Out-Null
    & $vbm startvm $nome --type headless 2>&1 | Out-Null
    Write-Host "  $nome iniciada, bootando do DVD"
}

Write-Host "`nInstalacao iniciada. Apos concluir, reduza para 1024 MB:"
Write-Host "  VBoxManage modifyvm VM1 --memory 1024"
