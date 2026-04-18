export type LeaderboardEntry = {
  rank: number;
  team: string;
  team_id?: string;
  equity: number;
  rud?: number;
  inventory_value?: number;
  bailout_penalty?: number;
};

export type BookLevel = { price: number; quantity: number; owner?: string };

export type Book = {
  symbol: string;
  bids: BookLevel[];
  asks: BookLevel[];
  last_price?: number;
  tick?: number;
};

export type PublicBookResponse = {
  tick?: number;
  books: Book[] | Record<string, Book>;
};

export type Trade = {
  symbol: string;
  price: number;
  quantity: number;
  ts: number;
  tick?: number;
  side?: "buy" | "sell";
};

export type TapeEvent = {
  symbol: string;
  side: "buy" | "sell";
  price: number;
  quantity: number;
  reason: string;
  ts: number;
  tick?: number;
};

export type TimeseriesMeta = {
  name: string;
  status?: string;
  latest_value?: number;
  latest_ts?: number;
  unit?: string;
};

export type TimeseriesPoint = { t: number; v: number };
