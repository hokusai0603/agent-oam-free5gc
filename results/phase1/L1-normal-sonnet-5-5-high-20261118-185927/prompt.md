你在一台 Ubuntu 22.04 的機器上，上面已經有一個單節點的 K3s 叢集（`kubectl`、`helm` 都可以直接用，需要 root 時可以用 `sudo`）。這台機器的 kernel 已經載入 gtp5g 模組。

請在這個叢集上用 free5GC 部署一個最基本的 5G SA 網路，並用 UERANSIM 模擬基地台與手機：

- 1 個 gNB、1 個 UE、1 個 UPF
- PLMN：MCC 208、MNC 93
- 1 個 slice：SST 1、SD 010203
- 1 個 DNN：`internet`
- UE 的 SUPI：`imsi-208930000000001`
- 版本：free5GC **v4.3.0**。Helm chart 請用 https://github.com/free5gc/free5gc-helm 的 commit `07139fcc6f7579928a0722e274bdc876a4a4e240`（free5GC v4.3.0 對應的版本），不要用其他版本或 `main` 的最新版

完成條件：UE 要成功註冊並建立 PDU session，而且能透過 5G 網路（UE 的 tunnel 介面）連到網際網路，例如 ping 8.8.8.8。

這是無人值守的工作：不會有人回答你的問題，請自己判斷、自己完成。

完成後請說明：(1) 你部署了哪些元件、用了哪些設定；(2) 你怎麼確認這個網路是正確的。
