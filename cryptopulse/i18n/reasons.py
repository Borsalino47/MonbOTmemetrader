"""The French catalogue. Every sentence a score can say about itself.

ORGANISED BY THE ENGINE THAT SAYS IT, NOT ALPHABETICALLY, so that reading one
block tells you everything a component can conclude.

TWO REGISTERS, ONE MEANING

Some entries carry a `_SIMPLE` variant. The technical wording is correct and
compact for someone who reads charts; the simple wording says the same thing to
someone who does not (spec §9-10). They must never state different facts — only
the same fact at different depths — because a user switching modes would
otherwise read two different opinions of one number.

ACRONYMS SURVIVE

RVOL, RSI, ATR, MACD, MFE, MAE, FDV and PnL stay as they are: they are the
vocabulary of the field and translating them would make the app harder to use,
not easier (spec §8). What changes is that they are always accompanied by
something that says what they mean.
"""

from __future__ import annotations

from cryptopulse.i18n import msg

# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #

VOLUME_UNAVAILABLE = msg(
    "VOLUME_UNAVAILABLE",
    "Données de volume indisponibles (historique insuffisant)",
)
RVOL_HIGH = msg(
    "RVOL_HIGH",
    "RVOL {rvol} par rapport à la moyenne des {bars} dernières bougies",
)
RVOL_MILD = msg(
    "RVOL_MILD",
    "Volume modérément élevé : RVOL {rvol}",
)
RVOL_LOW = msg(
    "RVOL_LOW",
    "Volume insuffisant : RVOL {rvol}",
)
RVOL_LOW_SIMPLE = msg(
    "RVOL_LOW_SIMPLE",
    "⚠️ Volume faible : seulement {rvol} le volume habituel",
)
VOLUME_ACCELERATING = msg(
    "VOLUME_ACCELERATING",
    "Volume en accélération : {change} par rapport à la moyenne des {bars} dernières bougies",
)
RVOL_RISING = msg(
    "RVOL_RISING",
    "Le RVOL progresse bougie après bougie : il ne se contente pas d'être élevé",
)
VOLUME_CLIMAX = msg(
    "VOLUME_CLIMAX",
    "Volume en climax : {rvol} — souvent un sommet plutôt qu'un départ",
)

# --------------------------------------------------------------------------- #
# Momentum
# --------------------------------------------------------------------------- #

MOMENTUM_UNAVAILABLE = msg("MOMENTUM_UNAVAILABLE", "Données de momentum indisponibles")
MOMENTUM_FLAT = msg(
    "MOMENTUM_FLAT",
    "Momentum sans relief sur toutes les mesures examinées",
)
PRICE_ROC_UP = msg(
    "PRICE_ROC_UP",
    "Prix {change} sur les {bars} dernières bougies",
)
PRICE_ROC_DOWN = msg(
    "PRICE_ROC_DOWN",
    "Prix {change} sur les {bars} dernières bougies",
)
MACD_EXPANDING = msg(
    "MACD_EXPANDING",
    "Histogramme MACD en expansion : l'écart se creuse en faveur des acheteurs",
)
MACD_EXPANDING_SIMPLE = msg(
    "MACD_EXPANDING_SIMPLE",
    "Indicateur de tendance (MACD) en amélioration",
)
RSI_CONSTRUCTIVE = msg(
    "RSI_CONSTRUCTIVE",
    "RSI à {rsi} : zone constructive, ni survendu ni suracheté",
)
RSI_STRETCHED = msg(
    "RSI_STRETCHED",
    "RSI à {rsi} : marché fortement étiré, proche d'une zone de surachat",
)
RSI_STRETCHED_SIMPLE = msg(
    "RSI_STRETCHED_SIMPLE",
    "⚠️ RSI très élevé ({rsi}) : le prix a déjà fortement progressé et peut être "
    "plus vulnérable à un repli",
)

# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #

STRUCTURE_UNAVAILABLE = msg(
    "STRUCTURE_UNAVAILABLE",
    "Structure indisponible (historique insuffisant)",
)
STRUCTURE_UP = msg(
    "STRUCTURE_UP",
    "Structure haussière : sommets et creux de plus en plus hauts sur l'unité de temps principale",
)
STRUCTURE_UP_EXPERT = msg(
    "STRUCTURE_UP_EXPERT",
    "Structure HH/HL confirmée sur l'unité de temps principale "
    "(sommets plus hauts, creux plus hauts)",
)
STRUCTURE_DOWN = msg(
    "STRUCTURE_DOWN",
    "Structure baissière : sommets et creux de plus en plus bas — la structure joue contre le setup",
)
RANGE_TOP = msg(
    "RANGE_TOP",
    "Le prix se situe déjà dans la partie haute de sa zone de fluctuation récente",
)
SUPPORT_CLOSE = msg(
    "SUPPORT_CLOSE",
    "Support situé {distance} ATR sous le prix : niveau d'invalidation clairement défini",
)
SUPPORT_FAR = msg(
    "SUPPORT_FAR",
    "Le support le plus proche se situe à {distance} ATR : aucun niveau d'invalidation "
    "proche permettant de limiter clairement le risque",
)
FAILED_BREAKOUT_RECENT = msg(
    "FAILED_BREAKOUT_RECENT",
    "Fausse cassure récente : une tentative a déjà échoué sur ce niveau",
)
RETEST_HOLDING = msg(
    "RETEST_HOLDING",
    "Le prix conserve un niveau précédemment franchi lors du retest",
)

# --------------------------------------------------------------------------- #
# Breakout geometry
# --------------------------------------------------------------------------- #

BREAKOUT_UNAVAILABLE = msg(
    "BREAKOUT_UNAVAILABLE",
    "Géométrie de cassure indisponible (ni ATR ni structure)",
)
LEVEL_BROKEN = msg(
    "LEVEL_BROKEN",
    "Niveau déjà franchi sur une bougie clôturée",
)
NO_RESISTANCE = msg(
    "NO_RESISTANCE",
    "Aucune résistance identifiée au-dessus sur la période observée",
)
BELOW_RESISTANCE = msg(
    "BELOW_RESISTANCE",
    "À {distance} ATR sous la résistance (testée {touches} fois)",
)
RESISTANCE_FAR = msg(
    "RESISTANCE_FAR",
    "Résistance encore éloignée de {distance} ATR",
)

# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #

VOLATILITY_UNAVAILABLE = msg("VOLATILITY_UNAVAILABLE", "Profil de volatilité indisponible")
BB_COMPRESSED = msg(
    "BB_COMPRESSED",
    "Bandes de Bollinger dans les {percent} les plus resserrés de leur amplitude récente",
)
BB_COMPRESSED_SIMPLE = msg(
    "BB_COMPRESSED_SIMPLE",
    "Le prix est très comprimé : ce type de calme précède souvent un mouvement",
)
COMPRESSION_RELEASING = msg(
    "COMPRESSION_RELEASING",
    "La compression se libère avec du volume : configuration d'expansion",
)

# --------------------------------------------------------------------------- #
# Order flow
# --------------------------------------------------------------------------- #

BOOK_UNAVAILABLE = msg(
    "BOOK_UNAVAILABLE",
    "Carnet d'ordres non récupéré pour cet actif (donnée indisponible)",
)
BID_DOMINANT = msg(
    "BID_DOMINANT",
    "Profondeur à l'achat dominante ({imbalance} de déséquilibre à 0,5 % du prix médian)",
)
ASK_DOMINANT = msg(
    "ASK_DOMINANT",
    "Profondeur à la vente dominante ({imbalance}) : vendeurs empilés au-dessus",
)
BOOK_BALANCED = msg("BOOK_BALANCED", "Carnet d'ordres globalement équilibré")

# --------------------------------------------------------------------------- #
# Multi-timeframe
# --------------------------------------------------------------------------- #

MTF_TOO_FEW = msg("MTF_TOO_FEW", "Moins de deux unités de temps disponibles")
MTF_BULLISH = msg("MTF_BULLISH", "Orientation haussière sur {timeframes}")
MTF_BEARISH = msg("MTF_BEARISH", "Orientation baissière sur {timeframes}")
MTF_NEUTRAL = msg(
    "MTF_NEUTRAL",
    "Aucun accord directionnel : les {count} unités de temps sont neutres",
)

# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #

LIQUIDITY_UNAVAILABLE = msg("LIQUIDITY_UNAVAILABLE", "Mesures de liquidité indisponibles")
LIQUIDITY_VOLUME_24H = msg("LIQUIDITY_VOLUME_24H", "Volume 24 h en devise de cotation : {volume}")
LIQUIDITY_NO_VOLUME = msg(
    "LIQUIDITY_NO_VOLUME",
    "Liquidité évaluée sans chiffre de volume 24 h",
)
SPREAD_TIGHT = msg("SPREAD_TIGHT", "Écart achat/vente serré : {spread} points de base")
SPREAD_WIDE = msg("SPREAD_WIDE", "Écart achat/vente large : {spread} points de base")
LIQUIDITY_DANGEROUS = msg(
    "LIQUIDITY_DANGEROUS",
    "Liquidité dangereuse : entrer serait facile, sortir ne le serait pas",
)

# --------------------------------------------------------------------------- #
# Triggers and invalidations
# --------------------------------------------------------------------------- #

TRIGGER_RECLAIM = msg(
    "TRIGGER_RECLAIM",
    "Repasser au-dessus du niveau cassé et s'y maintenir, avec confirmation du volume",
)
TRIGGER_BREAK_ABOVE = msg(
    "TRIGGER_BREAK_ABOVE",
    "Franchir {level} en clôture de bougie, avec volume à l'appui",
)
TRIGGER_ALREADY_FIRED = msg(
    "TRIGGER_ALREADY_FIRED",
    "Déclenchement déjà effectué : cassure actuellement en cours",
)
TRIGGER_NONE = msg(
    "TRIGGER_NONE",
    "Aucun déclencheur défini à ce stade",
)
INVALIDATION_BELOW = msg(
    "INVALIDATION_BELOW",
    "Clôture sous {level} : la raison de l'achat ne tient plus",
)

# --------------------------------------------------------------------------- #
# Pump maturity
# --------------------------------------------------------------------------- #

MATURITY_NOT_STARTED = msg(
    "MATURITY_NOT_STARTED",
    "Le mouvement ne semble pas encore avoir commencé",
)
MATURITY_EXTENDED = msg(
    "MATURITY_EXTENDED",
    "{change} sur {window} : mouvement déjà très étendu",
)
MATURITY_ENGAGED = msg(
    "MATURITY_ENGAGED",
    "{change} sur {window} : mouvement bien engagé",
)
MATURITY_MOVING = msg("MATURITY_MOVING", "{change} sur {window}")
MATURITY_RETRACING = msg(
    "MATURITY_RETRACING",
    "{change} sur {window} : repli après hausse",
)
MATURITY_UNKNOWN = msg(
    "MATURITY_UNKNOWN",
    "Aucune donnée de mouvement : maturité inconnue, valeur neutre affichée",
)
MATURITY_CLIMAX = msg(
    "MATURITY_CLIMAX",
    "Volume actuel {ratio} la moyenne 24 h : climax probable",
)
MATURITY_VOLUME_HOT = msg("MATURITY_VOLUME_HOT", "Volume actuel {ratio} la moyenne 24 h")
MATURITY_SELLERS = msg(
    "MATURITY_SELLERS",
    "{share} de ventes sur 5 min : vendeurs majoritaires",
)
MATURITY_SELLERS_MILD = msg("MATURITY_SELLERS_MILD", "{share} de ventes sur 5 min")

# --------------------------------------------------------------------------- #
# Data confidence
# --------------------------------------------------------------------------- #

CONFIDENCE_STALE = msg(
    "CONFIDENCE_STALE",
    "Données âgées de {age} : la confiance est plafonnée quel que soit le reste",
)
CONFIDENCE_THIN_HISTORY = msg(
    "CONFIDENCE_THIN_HISTORY",
    "Historique court : {bars} bougies disponibles",
)
CONFIDENCE_MISSING_TF = msg(
    "CONFIDENCE_MISSING_TF",
    "Unités de temps manquantes : {timeframes}",
)

# --------------------------------------------------------------------------- #
# Pump maturity (CEX)
# --------------------------------------------------------------------------- #

PM_ROC_SHORT = msg("PM_ROC_SHORT", "{change} sur les {bars} dernières bougies")
PM_ROC_LONG = msg("PM_ROC_LONG", "{change} sur les {bars} dernières bougies")
PM_HTF_MOVE = msg("PM_HTF_MOVE", "Déjà {change} sur l'unité de temps supérieure")
PM_ABOVE_EMA = msg("PM_ABOVE_EMA", "{change} au-dessus de la moyenne mobile EMA20")
PM_STRETCHED = msg(
    "PM_STRETCHED",
    "Prix à {distance} ATR au-dessus de l'EMA20 : mouvement étiré",
)
PM_GREEN_RUN = msg("PM_GREEN_RUN", "{count} bougies haussières consécutives")
PM_RSI = msg("PM_RSI", "RSI à {rsi}")
PM_VOLUME_CLIMAX = msg(
    "PM_VOLUME_CLIMAX",
    "Volume en climax après un mouvement déjà étendu",
)
PM_INSUFFICIENT = msg(
    "PM_INSUFFICIENT",
    "Données insuffisantes pour évaluer la maturité du mouvement",
)
PM_NOT_EXTENDED = msg(
    "PM_NOT_EXTENDED",
    "Aucun signe que le mouvement soit déjà étendu",
)

# --------------------------------------------------------------------------- #
# Score acceleration
# --------------------------------------------------------------------------- #

ACC_RVOL_CLIMBING = msg("ACC_RVOL_CLIMBING", "RVOL en progression bougie après bougie")
ACC_COMPRESSION_RELEASING = msg(
    "ACC_COMPRESSION_RELEASING",
    "Volatilité comprimée et commençant à se libérer",
)
ACC_INSUFFICIENT = msg(
    "ACC_INSUFFICIENT",
    "Historique insuffisant pour mesurer une accélération",
)
ACC_EARLY_CHANGE = msg(
    "ACC_EARLY_CHANGE",
    "Le comportement change alors que le mouvement est encore jeune",
)

# --------------------------------------------------------------------------- #
# Liquidity gate
# --------------------------------------------------------------------------- #

LIQ_NO_VOLUME_DATA = msg(
    "LIQ_NO_VOLUME_DATA",
    "Volume 24 h indisponible : impossible d'évaluer la liquidité",
)
LIQ_BELOW_FLOOR = msg(
    "LIQ_BELOW_FLOOR",
    "Volume 24 h de {volume}, sous le plancher de {floor}",
)
LIQ_SPREAD_FATAL = msg(
    "LIQ_SPREAD_FATAL",
    "Écart achat/vente de {spread} points de base : le seul coût d'exécution invalide le setup",
)
LIQ_SPREAD_WIDE = msg("LIQ_SPREAD_WIDE", "Écart achat/vente large ({spread} points de base)")
LIQ_SPREAD_UNKNOWN = msg(
    "LIQ_SPREAD_UNKNOWN",
    "Écart inconnu (pas de carnet d'ordres) : liquidité jugée sur le seul volume",
)
LIQ_GRADED = msg("LIQ_GRADED", "Liquidité {status} sur un volume 24 h de {volume}")

# --------------------------------------------------------------------------- #
# Explosion 15m (CEX)
# --------------------------------------------------------------------------- #

EXP_NO_RVOL = msg("EXP_NO_RVOL", "Donnée indisponible : aucun volume relatif sur 5 min ni 15 min")
EXP_TF_VOLUME_HIGH = msg("EXP_TF_VOLUME_HIGH", "Volume {timeframe} à {rvol} de sa propre moyenne")
EXP_TF_VOLUME_LOW = msg("EXP_TF_VOLUME_LOW", "Volume {timeframe} sous sa moyenne ({rvol})")
EXP_RVOL_FALLING = msg(
    "EXP_RVOL_FALLING",
    "Le volume relatif 5 min retombe déjà : la poussée est peut-être passée",
)
EXP_VOLUME_ELEVATED = msg("EXP_VOLUME_ELEVATED", "Volume élevé sur les unités de temps courtes")
EXP_NO_SHORT_HISTORY = msg(
    "EXP_NO_SHORT_HISTORY",
    "Donnée indisponible : pas d'historique de prix court terme",
)
EXP_NO_ROC = msg(
    "EXP_NO_ROC",
    "Donnée indisponible : la variation récente n'a pas pu être calculée",
)
EXP_ROC_UP = msg("EXP_ROC_UP", "Prix {change} sur les trois dernières bougies de 5 min")
EXP_ROC_DOWN = msg("EXP_ROC_DOWN", "Prix en baisse ({change} sur 5 min)")
EXP_LAST_BAR = msg("EXP_LAST_BAR", "Dernière bougie de 15 min à {change}")
EXP_SPEEDING_UP = msg("EXP_SPEEDING_UP", "Le mouvement accélère au lieu de s'essouffler")
EXP_SLOWING = msg("EXP_SLOWING", "La dernière bougie est plus lente que les trois précédentes")
EXP_PRICE_MOVING = msg("EXP_PRICE_MOVING", "Le prix bouge sur les unités de temps courtes")
EXP_NO_RESISTANCE = msg(
    "EXP_NO_RESISTANCE",
    "Donnée indisponible : aucune résistance mesurable sur 5 min ni 15 min",
)
EXP_THROUGH_RESISTANCE = msg(
    "EXP_THROUGH_RESISTANCE",
    "Résistance {timeframe} déjà franchie : le mouvement est en cours",
)
EXP_NEAR_RESISTANCE = msg(
    "EXP_NEAR_RESISTANCE",
    "À {distance} ATR sous la résistance {timeframe} : atteignable dans la fenêtre",
)
EXP_UNDER_RESISTANCE = msg(
    "EXP_UNDER_RESISTANCE",
    "À {distance} ATR sous la résistance {timeframe}",
)
EXP_RESISTANCE_REACHABLE = msg(
    "EXP_RESISTANCE_REACHABLE",
    "À {distance} ATR de la résistance : atteignable, mais pas en quinze minutes",
)
EXP_RESISTANCE_TOO_FAR = msg(
    "EXP_RESISTANCE_TOO_FAR",
    "À {distance} ATR de la résistance : rien ne peut se produire ici dans la fenêtre",
)
EXP_FAILED_BREAKOUT = msg(
    "EXP_FAILED_BREAKOUT",
    "La dernière cassure a échoué : ce niveau a déjà rejeté le prix une fois",
)
EXP_NO_COMPRESSION = msg(
    "EXP_NO_COMPRESSION",
    "Donnée indisponible : la compression de volatilité n'a pas pu être mesurée",
)
EXP_RANGE_NEUTRAL = msg(
    "EXP_RANGE_NEUTRAL",
    "Amplitude {timeframe} ni comprimée ni étirée",
)
EXP_NO_BOOK = msg(
    "EXP_NO_BOOK",
    "Aucun carnet d'ordres récupéré pour ce symbole : pression inconnue",
)
EXP_BIDS_OUTWEIGH = msg("EXP_BIDS_OUTWEIGH", "Les achats en attente dépassent les ventes ({imbalance})")
EXP_ASKS_OUTWEIGH = msg("EXP_ASKS_OUTWEIGH", "Les ventes en attente dépassent les achats ({imbalance})")
EXP_SPREAD_UNKNOWN = msg("EXP_SPREAD_UNKNOWN", "Écart achat/vente inconnu")
EXP_SPREAD_OK = msg(
    "EXP_SPREAD_OK",
    "Écart de {spread} points de base : un mouvement rapide ne serait pas absorbé par le coût",
)
EXP_SPREAD_COSTLY = msg(
    "EXP_SPREAD_COSTLY",
    "Écart de {spread} points de base : un mouvement de 15 min serait en grande partie du coût",
)
EXP_MATURITY_UNKNOWN = msg(
    "EXP_MATURITY_UNKNOWN",
    "Donnée indisponible : maturité du mouvement inconnue",
)
EXP_BARELY_STARTED = msg(
    "EXP_BARELY_STARTED",
    "Mouvement à peine commencé (maturité {maturity}/100)",
)
EXP_UNDER_WAY = msg(
    "EXP_UNDER_WAY",
    "Mouvement en cours mais pas encore étendu (maturité {maturity}/100)",
)
EXP_RSI_UNAVAILABLE = msg("EXP_RSI_UNAVAILABLE", "RSI court terme indisponible")
EXP_RSI_STRETCHED = msg("EXP_RSI_STRETCHED", "RSI {timeframe} à {rsi} : étiré")
EXP_RSI_ROOM = msg("EXP_RSI_ROOM", "RSI {timeframe} à {rsi} : marge avant épuisement")

# --------------------------------------------------------------------------- #
# Safety (CEX market quality)
# --------------------------------------------------------------------------- #

SAF_LIQ_DANGEROUS = msg("SAF_LIQ_DANGEROUS", "Liquidité dangereuse : veto absolu")
SAF_LIQ_POOR = msg("SAF_LIQ_POOR", "Liquidité faible")
SAF_LIQ_UNKNOWN = msg("SAF_LIQ_UNKNOWN", "La liquidité n'a pas pu être évaluée")
SAF_GAPS = msg(
    "SAF_GAPS",
    "Trous dans le flux de bougies : la paire ne se négocie pas en continu",
)
SAF_SHORT_HISTORY = msg("SAF_SHORT_HISTORY", "Seulement {bars} bougies d'historique")
SAF_VOL_EXTREME = msg(
    "SAF_VOL_EXTREME",
    "Volatilité extrême : ATR de {atr} du prix par bougie",
)
SAF_VOL_HIGH = msg("SAF_VOL_HIGH", "Volatilité élevée : ATR de {atr} du prix")
SAF_SPREAD = msg("SAF_SPREAD", "Écart achat/vente de {spread} points de base")
SAF_BELOW_FLOOR = msg(
    "SAF_BELOW_FLOOR",
    "Score de sécurité de {score}, au niveau ou sous le plancher de {floor} : veto absolu",
)
SAF_CLEAN = msg("SAF_CLEAN", "Aucun défaut de qualité de marché détecté")

# --------------------------------------------------------------------------- #
# Token discovery (venue-wide behaviour change)
# --------------------------------------------------------------------------- #

DISC_NO_RVOL = msg("DISC_NO_RVOL", "Donnée indisponible : le volume relatif n'a pas pu être calculé")
DISC_RVOL_HIGH = msg("DISC_RVOL_HIGH", "Volume à {rvol} de la propre moyenne récente du token")
DISC_RVOL_LOW = msg(
    "DISC_RVOL_LOW",
    "Volume sous sa propre moyenne ({rvol}) : il ne se passe rien",
)
DISC_NO_VENUE_COMPARE = msg(
    "DISC_NO_VENUE_COMPARE",
    "Pas encore de comparaison à l'échelle du venue (deux relevés nécessaires)",
)
DISC_EXCESS_VS_YESTERDAY = msg(
    "DISC_EXCESS_VS_YESTERDAY",
    "Activité {change} au-dessus du même moment hier",
)
DISC_ABOVE_NORMAL = msg("DISC_ABOVE_NORMAL", "Activité au-dessus du niveau habituel de ce token")
DISC_NO_ACCEL_BARS = msg(
    "DISC_NO_ACCEL_BARS",
    "Donnée indisponible : pas assez de bougies pour mesurer une accélération",
)
DISC_RVOL_RISING = msg("DISC_RVOL_RISING", "Le volume relatif progresse encore")
DISC_RVOL_FALLING = msg(
    "DISC_RVOL_FALLING",
    "Le volume relatif retombe déjà : la poussée est peut-être terminée",
)
DISC_VOL_ACCEL = msg("DISC_VOL_ACCEL", "Volume en accélération ({change})")
DISC_VOL_DECEL = msg("DISC_VOL_DECEL", "Volume en décélération ({change})")
DISC_BUILDING = msg("DISC_BUILDING", "L'activité se construit au lieu de s'éteindre")
DISC_NOT_ELEVATED = msg(
    "DISC_NOT_ELEVATED",
    "Le volume n'est pas élevé : rien ne précède rien",
)
DISC_NO_ROC = msg("DISC_NO_ROC", "Donnée indisponible : variation récente inconnue")
DISC_VOLUME_LEADS = msg(
    "DISC_VOLUME_LEADS",
    "Volume élevé alors que le prix est encore plat : l'activité précède le prix",
)
DISC_VOLUME_LEADS_MILD = msg(
    "DISC_VOLUME_LEADS_MILD",
    "Volume élevé et prix qui commence tout juste à bouger",
)
DISC_PRICE_AHEAD = msg(
    "DISC_PRICE_AHEAD",
    "Le prix bouge déjà de {change} : le volume n'est pas en avance sur lui",
)
DISC_NO_COMPRESSION = msg(
    "DISC_NO_COMPRESSION",
    "Donnée indisponible : la compression de volatilité n'a pas pu être mesurée",
)
DISC_COMPRESSED = msg(
    "DISC_COMPRESSED",
    "Volatilité comprimée ({value}) : ressort armé plutôt qu'épuisé",
)
DISC_EXPANDED = msg(
    "DISC_EXPANDED",
    "Volatilité déjà large ({value}) : l'expansion a eu lieu",
)
DISC_VOL_NEUTRAL = msg("DISC_VOL_NEUTRAL", "Volatilité ni comprimée ni étirée")
DISC_MATURITY_UNKNOWN = msg(
    "DISC_MATURITY_UNKNOWN",
    "Donnée indisponible : maturité du mouvement inconnue",
)
DISC_BARELY_STARTED = msg("DISC_BARELY_STARTED", "Mouvement à peine commencé (maturité {maturity}/100)")
DISC_UNDER_WAY = msg("DISC_UNDER_WAY", "Mouvement en cours mais pas étendu (maturité {maturity}/100)")
DISC_ADVANCED = msg(
    "DISC_ADVANCED",
    "Mouvement déjà avancé (maturité {maturity}/100) : ce n'est pas une découverte précoce",
)
DISC_RANGE_UNKNOWN = msg("DISC_RANGE_UNKNOWN", "Position dans l'amplitude 24 h inconnue")
DISC_RANGE_ROOM = msg(
    "DISC_RANGE_ROOM",
    "À {position} de l'amplitude 24 h, avec de la marge au-dessus",
)
DISC_RANGE_HIGH = msg("DISC_RANGE_HIGH", "Déjà à {position} de l'amplitude 24 h")
DISC_NO_LIQUIDITY = msg("DISC_NO_LIQUIDITY", "Donnée indisponible : liquidité non évaluée")
DISC_LIQUIDITY_OK = msg("DISC_LIQUIDITY_OK", "Liquidité {status}")
DISC_LIQUIDITY_POOR = msg(
    "DISC_LIQUIDITY_POOR",
    "Liquidité {status} : un mouvement ici pourrait ne pas être négociable",
)
DISC_SPREAD_UNKNOWN = msg("DISC_SPREAD_UNKNOWN", "Écart achat/vente inconnu")
DISC_SPREAD_OK = msg("DISC_SPREAD_OK", "Écart de {spread} points de base")
DISC_SPREAD_WIDE = msg(
    "DISC_SPREAD_WIDE",
    "Un écart de {spread} points de base absorberait un petit mouvement",
)

# --------------------------------------------------------------------------- #
# Hunter pre-scan
# --------------------------------------------------------------------------- #

HUNT_NO_PREVIOUS = msg(
    "HUNT_NO_PREVIOUS",
    "Pas encore de relevé précédent : accélération inconnue",
)
HUNT_ACTIVITY_FLAT_PRICE = msg(
    "HUNT_ACTIVITY_FLAT_PRICE",
    "Activité en hausse alors que le prix est encore plat",
)
HUNT_RANGE_UNUSABLE = msg(
    "HUNT_RANGE_UNUSABLE",
    "Amplitude 24 h inexploitable (aucun écart entre le plus haut et le plus bas)",
)
HUNT_RANGE_ROOM = msg(
    "HUNT_RANGE_ROOM",
    "À {position} de l'amplitude 24 h, pas collé au plus haut",
)
HUNT_RANGE_HIGH = msg("HUNT_RANGE_HIGH", "Déjà à {position} de l'amplitude 24 h")
HUNT_MOVE_SPENT = msg(
    "HUNT_MOVE_SPENT",
    "Déjà {change} sur 24 h : le mouvement est peut-être épuisé",
)
HUNT_MOVE_DONE = msg("HUNT_MOVE_DONE", "Déjà {change} sur 24 h")
HUNT_NO_SPREAD = msg(
    "HUNT_NO_SPREAD",
    "Écart inconnu : ce venue ne publie pas d'achat/vente ici",
)
HUNT_SPREAD_TIGHT = msg("HUNT_SPREAD_TIGHT", "Écart serré ({spread} points de base)")
HUNT_SPREAD_WIDE = msg(
    "HUNT_SPREAD_WIDE",
    "Un écart large ({spread} points de base) absorberait un petit mouvement",
)

# --------------------------------------------------------------------------- #
# Data confidence issues
# --------------------------------------------------------------------------- #

CONF_STALE = msg("CONF_STALE", "Données périmées : la bougie principale a clôturé il y a {age}")
CONF_SHORT = msg("CONF_SHORT", "Historique insuffisant : {bars} bougies clôturées")
CONF_MISSING_TF = msg("CONF_MISSING_TF", "Seulement {got}/{wanted} unités de temps disponibles")
CONF_PARTIAL_TF = msg("CONF_PARTIAL_TF", "{timeframe} : série incomplète")
CONF_NO_BOOK = msg("CONF_NO_BOOK", "Carnet d'ordres indisponible")
CONF_CAPPED = msg(
    "CONF_CAPPED",
    "Confiance plafonnée à {cap} car les données datent de {age}",
)

# --------------------------------------------------------------------------- #
# Compression detail
# --------------------------------------------------------------------------- #

BB_PARTIAL = msg(
    "BB_PARTIAL",
    "Largeur des bandes de Bollinger au {percentile}ᵉ centile de son amplitude récente "
    "(compression partielle)",
)
BB_NONE = msg(
    "BB_NONE",
    "Largeur des bandes de Bollinger au {percentile}ᵉ centile de son amplitude récente "
    "(aucune compression à libérer)",
)

# --------------------------------------------------------------------------- #
# Setup triggers and invalidations
# --------------------------------------------------------------------------- #

TRG_VETO = msg("TRG_VETO", "Le veto doit être levé avant que ce setup puisse être suivi")
TRG_REQUALIFY = msg(
    "TRG_REQUALIFY",
    "Nécessite un creux plus haut et un retour au-dessus de l'EMA20 pour se requalifier",
)
TRG_ALREADY_INVALID = msg("TRG_ALREADY_INVALID", "Déjà invalidé par la tentative échouée")
TRG_RECLAIM_HOLD = msg(
    "TRG_RECLAIM_HOLD",
    "Repasser au-dessus du niveau cassé et s'y maintenir, avec confirmation du volume",
)
TRG_CLOSE_BELOW_RETEST = msg(
    "TRG_CLOSE_BELOW_RETEST",
    "Une clôture repassant sous {level} invalide le retest",
)
TRG_CONTINUED_CLOSES = msg(
    "TRG_CONTINUED_CLOSES",
    "Clôtures successives au-dessus du niveau cassé, avec volume soutenu",
)
TRG_LOSE_LEVEL = msg(
    "TRG_LOSE_LEVEL",
    "Perte du niveau franchi, ou cassure du dernier creux plus haut",
)
TRG_IN_PROGRESS = msg("TRG_IN_PROGRESS", "Déclenchement déjà effectué : cassure actuellement en cours")
TRG_CLOSE_BELOW_FAILS = msg(
    "TRG_CLOSE_BELOW_FAILS",
    "Une clôture repassant sous {level} ferait échouer la cassure",
)
TRG_REJECTION = msg("TRG_REJECTION", "Rejet du niveau avec volume en hausse")
TRG_CLOSE_BELOW = msg("TRG_CLOSE_BELOW", "Une clôture sous {level}")
TRG_APPROACH = msg(
    "TRG_APPROACH",
    "Approche de la résistance identifiée avec un volume encore en hausse",
)

HUNT_VOLUME_VS_YESTERDAY = msg(
    "HUNT_VOLUME_VS_YESTERDAY",
    "Échanges {change} au-dessus du même moment hier",
)
HUNT_TRADES_VS_YESTERDAY = msg(
    "HUNT_TRADES_VS_YESTERDAY",
    "Nombre de transactions {change} au-dessus d'hier",
)

EXP_ALREADY_MATURE = msg(
    "EXP_ALREADY_MATURE",
    "Mouvement déjà mûr ({maturity}/100) : les quinze prochaines minutes ont plus de "
    "chances de corriger que de prolonger",
)

# --------------------------------------------------------------------------- #
# Errors and channel availability, as the user reads them (spec §18)
# --------------------------------------------------------------------------- #

ERR_SOURCE_UNAVAILABLE = msg(
    "ERR_SOURCE_UNAVAILABLE",
    "Source de données temporairement indisponible",
)
ERR_RATE_LIMITED = msg(
    "ERR_RATE_LIMITED",
    "Limite de requêtes atteinte — nouvelle tentative automatique",
)
ERR_INSUFFICIENT_DATA = msg(
    "ERR_INSUFFICIENT_DATA",
    "Données insuffisantes pour produire une décision fiable",
)
ERR_STALE = msg("ERR_STALE", "Données trop anciennes pour être présentées comme actuelles")

NOTIF_NO_PACKAGE = msg(
    "NOTIF_NO_PACKAGE",
    "{binary} introuvable — le paquet termux-api n'est pas installé",
)
NOTIF_NO_TERMUX = msg(
    "NOTIF_NO_TERMUX",
    "{binary} introuvable — cet appareil ne fait pas tourner Termux avec termux-api",
)
NOTIF_ALREADY_SENT = msg(
    "NOTIF_ALREADY_SENT",
    "{symbol} : déjà notifié en {previous}, toujours en {level}",
)

EXP_RANGE_COMPRESSED = msg(
    "EXP_RANGE_COMPRESSED",
    "Amplitude {timeframe} comprimée ({value}) : ressort armé plutôt qu'épuisé",
)
EXP_RANGE_WIDEST = msg(
    "EXP_RANGE_WIDEST",
    "Amplitude {timeframe} déjà à son maximum ({value}) : l'expansion a eu lieu",
)

# --------------------------------------------------------------------------- #
# Setup rationales — the sentence under the state badge
# --------------------------------------------------------------------------- #

RAT_VETO = msg("RAT_VETO", "Veto actif (liquidité ou sécurité) : ne peut pas être promu")
RAT_NO_STRUCTURE = msg("RAT_NO_STRUCTURE", "Aucune lecture structurelle disponible")
RAT_DOWNTREND = msg("RAT_DOWNTREND", "Tendance structurelle baissière, biais principal vendeur")
RAT_FAILED = msg(
    "RAT_FAILED",
    "Tentative de cassure échouée et score non revenu à son niveau",
)
RAT_RETEST = msg(
    "RAT_RETEST",
    "Le prix est revenu sur un niveau qu'il avait franchi et le tient comme support",
)
RAT_CONTINUATION = msg(
    "RAT_CONTINUATION",
    "Mouvement déjà en cours et structure toujours intacte",
)
RAT_BREAKOUT = msg(
    "RAT_BREAKOUT",
    "Niveau franchi sur une bougie clôturée, avec confirmation du score",
)
RAT_RETEST_ABOVE = msg(
    "RAT_RETEST_ABOVE",
    "Le prix teste par le dessus un niveau précédemment franchi",
)
RAT_ARMED = msg(
    "RAT_ARMED",
    "Configuration préparatoire solide, à {distance} ATR de son niveau de déclenchement",
)
TRG_CLOSE_ABOVE = msg("TRG_CLOSE_ABOVE", "Une clôture au-dessus de {level}")
TRG_CLOSE_ABOVE_RESISTANCE = msg(
    "TRG_CLOSE_ABOVE_RESISTANCE",
    "Une clôture au-dessus de la résistance identifiée, avec volume soutenu",
)
INV_CLOSE_BELOW_SUPPORT = msg(
    "INV_CLOSE_BELOW_SUPPORT",
    "Une clôture sous le support à {level}",
)

# --------------------------------------------------------------------------- #
# Risk penalties — RAW - PENALTY = FINAL, every deduction named
# --------------------------------------------------------------------------- #

PEN_MATURITY_LATE = msg(
    "PEN_MATURITY_LATE",
    "Maturité du mouvement {maturity}/100 : mouvement probablement tardif",
)
PEN_MATURITY_PARTLY = msg(
    "PEN_MATURITY_PARTLY",
    "Maturité du mouvement {maturity}/100 : mouvement en partie joué",
)
PEN_LIQ_DANGEROUS = msg("PEN_LIQ_DANGEROUS", "Liquidité dangereuse")
PEN_LIQ_POOR = msg("PEN_LIQ_POOR", "Liquidité faible")
PEN_LIQ_UNKNOWN = msg("PEN_LIQ_UNKNOWN", "La liquidité n'a pas pu être évaluée")
PEN_SPREAD = msg(
    "PEN_SPREAD",
    "Un écart de {spread} points de base érode l'avantage",
)
PEN_VOLUME_CLIMAX = msg(
    "PEN_VOLUME_CLIMAX",
    "Volume en climax après un mouvement étendu : risque d'épuisement",
)
PEN_HTF_BOTH = msg(
    "PEN_HTF_BOTH",
    "1h et 4h toutes deux baissières : position contre l'unité de temps supérieure",
)
PEN_HTF_ONE = msg("PEN_HTF_ONE", "{timeframe} baissier")
PEN_FAKE_BREAKOUT = msg(
    "PEN_FAKE_BREAKOUT",
    "Tentative de cassure récente non tenue en clôture",
)
PEN_VOLATILITY = msg(
    "PEN_VOLATILITY",
    "ATR de {atr} du prix par bougie : le dimensionnement de position devient punitif",
)
PEN_CONFIDENCE = msg(
    "PEN_CONFIDENCE",
    "Confiance dans les données à {confidence}/100",
)

PEN_OVERHEAD_RESISTANCE = msg(
    "PEN_OVERHEAD_RESISTANCE",
    "Résistance fortement testée à seulement {distance} ATR au-dessus ({touches} touches)",
)

PEN_STRUCTURE_DOWN = msg(
    "PEN_STRUCTURE_DOWN",
    "Tendance structurelle baissière sur l'unité de temps principale",
)
PEN_MOMENTUM_DIVERGENCE = msg(
    "PEN_MOMENTUM_DIVERGENCE",
    "Prix sur les sommets de sa zone mais RSI sous 60 : divergence probable",
)

# Rationales for the three quiet states. They read on screen beside a symbol
# that is doing nothing in particular, which is most of them.
RAT_WATCH = msg(
    "RAT_WATCH",
    "Setup en formation, pas encore en position de se déclencher",
)
RAT_OBSERVE = msg(
    "RAT_OBSERVE",
    "Premiers signes d'un changement de comportement, pas encore exploitable",
)
RAT_IGNORE = msg(
    "RAT_IGNORE",
    "Rien de notable, ni structurellement ni en comportement",
)
TRG_SUSTAINED_VOLUME_SUFFIX = msg(
    "TRG_SUSTAINED_VOLUME_SUFFIX",
    " avec un volume soutenu",
)

INV_CLOSE_BELOW_ATR = msg(
    "INV_CLOSE_BELOW_ATR",
    "Une clôture sous {level} (1,5 ATR sous le prix actuel)",
)
TRG_CONFIRM_ATR_SUFFIX = msg(
    "TRG_CONFIRM_ATR_SUFFIX",
    " (+{atr} ATR = {level})",
)
