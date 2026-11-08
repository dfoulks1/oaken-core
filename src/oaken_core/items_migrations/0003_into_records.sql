-- The items ingestor keeps its items in the store's records now, not in its own table. Move the old rows (a row
-- stored before there were parts has its body as its one part) and drop the table.
INSERT INTO records (collection, id, data_json, rev, created_at, updated_at)
SELECT 'items', subject_id,
       json_object('source', source, 'body', json(body_json),
                   'parts', CASE WHEN parts_json = '{}' THEN json_object('body', json(body_json)) ELSE json(parts_json) END),
       1, created_at, created_at
FROM items_rows;
DROP TABLE items_rows;
