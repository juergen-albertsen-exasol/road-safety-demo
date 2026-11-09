-- Data space metadata: the offers this service uses and makes, and the contracts behind them (simulated, not
-- enforced: there is no connector in this iteration).
SELECT o."offer_id", o."provider_id", o."title", o."topic", o."usage_policy", c."status" AS "contract", c."agreed_on"
FROM ROAD_SAFETY.DATA_OFFERS o LEFT JOIN ROAD_SAFETY.CONTRACTS c ON c."offer_id" = o."offer_id"
ORDER BY CASE WHEN o."provider_id" = 'SERVICE' THEN 1 ELSE 0 END, o."provider_id"
