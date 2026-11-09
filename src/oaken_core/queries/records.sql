-- name: CreateRecord :exec
INSERT INTO records (collection, id, data_json, rev, created_at, updated_at)
VALUES (?, ?, ?, 1, ?, ?);

-- name: GetRecord :one
SELECT * FROM records WHERE collection = ? AND id = ?;

-- name: UpdateRecord :execrows
UPDATE records SET data_json = ?, rev = rev + 1, updated_at = ?
WHERE collection = ? AND id = ?;

-- name: DeleteRecord :execrows
DELETE FROM records WHERE collection = ? AND id = ?;
