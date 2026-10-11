from datetime import timedelta
import logging

DOMAIN = "eau_veolia"
LOGGER = logging.getLogger(__package__)

UPDATE_INTERVAL = timedelta(hours=3)
RECENT_DAYS = 35
OVERLAP_DAYS = 5

COGNITO_URL = "https://cognito-idp.eu-west-3.amazonaws.com"
COGNITO_CLIENT_ID = "3kghade1fg54739kj8pkbova8j"
BACKEND_URL = "https://prd-ael-sirius-backend.istefr.fr"
COMMUNES_URL = "https://prd-ael-sirius-refcommunes.istefr.fr"
TYPE_FRONT = "WEB_ORDINATEUR"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

CONF_WATER_PRICE = "prix_eau_potable"
CONF_WATER_FIXED = "abonnement_eau_potable"
CONF_SEWER_PRICE = "prix_assainissement"
CONF_SEWER_FIXED = "abonnement_assainissement"
CONF_INDEX_ORIGIN = "index_origine"
