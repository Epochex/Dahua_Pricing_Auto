"""
定价规则常量：DDP_RULES & PRICE_RULES

注意：
- DDP_RULES 的 key = 产品线 category（映射表里的 category）
- PRICE_RULES 的第一层 key = price_group_hint（映射表里的 price_group_hint）
"""

# =========================
# ① DDP A 计算规则
# =========================
DDP_RULES = {
    "IPC": (0.10, 0.008, 0.02, 0.000198),
    "HAC": (0.10, 0.008, 0.02, 0.000198),
    "PTZ": (0.10, 0.008, 0.02, 0.000198),
    "THERMAL": (0.10, 0.008, 0.02, 0.000198),

    "NVR": (0.10, 0.052, 0.02, 0.000198),
    "IVSS": (0.10, 0.052, 0.02, 0.000198),
    "EVS": (0.10, 0.052, 0.02, 0.000198),
    "XVR": (0.10, 0.052, 0.02, 0.000198),

    "TRANSMISSION": (0.10, 0.0, 0.02, 0.000198),
    "IT交换机路由器": (0.10, 0.0, 0.02, 0.000198),
    "VDP": (0.10, 0.0, 0.02, 0.000198),

    "ALARM": (0.05, 0.0, 0.02, 0.000198),

    "ACCESS CONTROL": (0.10, 0.021, 0.02, 0.000198),

    "ACCESSORY": (0.10, 0.011, 0.02, 0.000198),
    "ACCESSORY线缆": (0.10, 0.037, 0.02, 0.000198),

    "监视器": (0.10, 0.0, 0.02, 0.000198),
    "IT监视器": (0.10, 0.0, 0.02, 0.000198),
    "商显/TV-WALL": (0.10, 0.14, 0.02, 0.000198),

    "键盘/解码器": (0.10, 0.034, 0.02, 0.000198),
    "交通": (0.10, 0.008, 0.02, 0.000198),
    "车载前端": (0.10, 0.008, 0.02, 0.000198),
    "车载后端": (0.10, 0.052, 0.02, 0.000198),

    "硬盘/存储介质": (0.10, 0.0, 0.02, 0.000198),

    "视频会议": (0.10, 0.034, 0.02, 0.000198),

    "电子防盗门": (0.10, 0.0, 0.02, 0.000198),
    "安检机": (0.15, 0.0, 0.02, 0.000198),
    "电子白板": (0.15, 0.034, 0.02, 0.000198),
    "烟感": (0.10, 0.02, 0.02, 0.000198),
}

# =========================
# ② 渠道价规则（基于 DDP A）
# =========================
PRICE_RULES = {
    # IPC
    "IPC": {
        "PSDW":        dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50),
        "针孔":         dict(reseller=0.12, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "IPC5":        dict(reseller=0.12, gold=0.27, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "IPC5/7/MULTI-SENSOR / SPECIAL":
                       dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "IPC3-S2":     dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "IPC2-PRO":    dict(reseller=0.12, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "IPC2":        dict(reseller=0.12, gold=0.20, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "IPC1":        dict(reseller=0.12, gold=0.20, silver=0.25, ivory=0.30, msrp_on_installer=0.60),
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
    },
    # HAC
    "HAC": {
        "_default_":   dict(reseller=0.12, gold=0.17, silver=0.20, ivory=0.25, msrp_on_installer=0.60),
    },
    # PTZ
    "PTZ": {
        "PTZ/SDT/EXPLOSION PROOF / SD10/8 / 7":
                       dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50),
        "SD6/5/4/3/2/1":
                       dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50),
    },
    # Thermal
    "THERMAL": {
        "TPC":         dict(reseller=0.12, gold=0.20, silver=0.25, ivory=0.30, msrp_on_installer=0.20),
        "TPC4 TPC5":   dict(reseller=0.12, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.20),
        "_default_":   dict(reseller=0.12, gold=0.20, silver=0.25, ivory=0.30, msrp_on_installer=0.20),
    },
    # NVR / IVSS / EVS / XVR
    "NVR": {
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50),
    },
    "IVSS": {
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.50),
    },
    "EVS": {
        "_default_":   dict(reseller=0.00, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.40),
    },
    "XVR": {
        "_default_":   dict(reseller=0.12, gold=0.17, silver=0.20, ivory=0.25, msrp_on_installer=0.60),
    },
    # VDP
    "VDP": {
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.29, ivory=0.35, msrp_on_installer=0.50),
    },
    # Access Control
    "ACCESS CONTROL": {
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.35, ivory=0.40, msrp_on_installer=0.60),
    },
    # Alarm
    "ALARM": {
        "_default_":   dict(reseller=0.30, gold=0.30, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
    },
    # Transmission & IT 网络
    "TRANSMISSION": {
        "_default_":   dict(reseller=0.12, gold=0.20, silver=0.23, ivory=0.25, msrp_on_installer=0.60),
    },
    "TRANSMISSION L3": {
        "_default_":   dict(reseller=0.12, gold=0.20, silver=0.23, ivory=0.25, msrp_on_installer=0.40),
    },
    "无线网桥": {
        "_default_":   dict(reseller=0.12, gold=0.16, silver=0.21, ivory=0.26, msrp_on_installer=0.40),
    },
    # Accessory
    "ACCESSORY": {
        "ACCESSORY":   dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
        "ACCESSORY 线缆":
                       dict(reseller=0.12, gold=0.22, silver=0.25, ivory=0.30, msrp_on_installer=0.60),
        "_default_":   dict(reseller=0.12, gold=0.22, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
    },
    # 监视器 / 商显 / LCD
    "监视器/商显/LCD": {
        "_default_":   dict(reseller=0.12, gold=0.15, silver=0.20, ivory=0.25, msrp_on_installer=0.40),
    },
    "CCTV监视器": {
        "_default_":   dict(reseller=0.12, gold=0.20, silver=0.22, ivory=0.25, msrp_on_installer=0.40),
    },
    "IT监视器": {
        "_default_":   dict(reseller=0.12, gold=0.15, silver=0.15, ivory=0.15, msrp_on_installer=0.20),
    },
    # 键盘/解码器
    "键盘/解码器": {
        "_default_":   dict(reseller=0.15, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.40),
    },
    # 交通 / 停车场
    "交通/停车场": {
        "_default_":   dict(reseller=0.15, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.40),
    },
    # 车载
    "车载": {
        "_default_":   dict(reseller=0.15, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.40),
    },
    # 软件
    "软件": {
        "_default_":   dict(reseller=0.15, gold=0.25, silver=0.30, ivory=0.35, msrp_on_installer=0.60),
    },
    # 硬盘/存储介质
    "硬盘/存储介质": {
        "_default_":   dict(reseller=0.10, gold=0.10, silver=0.15, ivory=0.20, msrp_on_installer=0.20),
    },
    # 电子白板
    "电子白板": {
        "_default_":   dict(reseller=0.10, gold=0.15, silver=0.15, ivory=0.15, msrp_on_installer=0.20),
    },
    # 安检
    "安检": {
        "_default_":   dict(reseller=0.15, gold=0.30, silver=0.35, ivory=0.40, msrp_on_installer=0.40),
    },
    # EAS
    "EAS": {
        "_default_":   dict(reseller=0.12, gold=0.15, silver=0.20, ivory=0.25, msrp_on_installer=0.40),
    },
    # ESL
    "ESL": {
        "_default_":   dict(reseller=0.05, gold=0.05, silver=0.10, ivory=0.15, msrp_on_installer=0.40),
    },
    # 充电桩
    "充电桩": {
        "_default_":   dict(reseller=None, gold=0.15, silver=0.15, ivory=0.15, msrp_on_installer=0.20),
    },
}
