-- v3: 좌/우 그린 (한 홀에 그린이 둘인 구장 — 예: 베르힐 영종)
ALTER TABLE holes ADD COLUMN greens_m text;
ALTER TABLE hole_scores ADD COLUMN green text;
