-- What a formatter made of the body: {part name: content} as JSON.
ALTER TABLE items_rows ADD COLUMN parts_json TEXT NOT NULL DEFAULT '{}';
