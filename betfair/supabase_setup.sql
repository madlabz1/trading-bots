-- Run this in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS bot_state (
  bot_id        TEXT PRIMARY KEY,
  bot_name      TEXT NOT NULL,
  total_pl      FLOAT DEFAULT 0,
  open_positions JSONB DEFAULT '[]',
  last_run      TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Allow public read (for Vercel dashboard)
ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "public read" ON bot_state FOR SELECT USING (true);
CREATE POLICY "service write" ON bot_state FOR ALL USING (true);

-- Insert initial rows for all 5 bots
INSERT INTO bot_state (bot_id, bot_name) VALUES
  ('bot1', 'Market Maker'),
  ('bot2', 'Momentum'),
  ('bot3', 'Sharp Copy'),
  ('bot4', 'News Trigger'),
  ('bot5', 'Value Scanner')
ON CONFLICT (bot_id) DO NOTHING;
