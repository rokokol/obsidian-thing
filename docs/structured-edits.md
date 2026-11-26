# Edit Canvas and Base files

Call `obsidian.agentsmd` before the first edit. Read the file with `obsidian.read` and pass its `revision` as `expected_revision`. The tools apply `changes` in order and validate the final document. If any step fails, the file stays unchanged

Use `obsidian.write` to create a file. You can also use `obsidian.write` and `obsidian.update` to edit one. All write tools check document structure and run the configured write checks before they replace a file

## Canvas

Call `obsidian.canvas_edit` with `path`, `expected_revision` and `changes`

| `op` | Fields | Effect |
| --- | --- | --- |
| `put_node` | `id`, `fields` | Add a node or update the named fields of an existing node |
| `put_edge` | `id`, `fields` | Add an edge or update the named fields of an existing edge |
| `remove_node` | `id` | Remove the node. Remove its edges in the same batch |
| `remove_edge` | `id` | Remove the edge |

A new node needs `type`, coordinates, dimensions, and the fields for its node type. A new edge needs `fromNode` and `toNode`. Both values must name nodes in the final canvas. Node and edge IDs must be unique across the document

## Bases

Call `obsidian.base_edit` with `path`, `expected_revision` and `changes`

| `op` | Fields | Effect |
| --- | --- | --- |
| `set_filters` | `value`, optional `view` | Set global filters or filters for one view. Pass `null` to remove them |
| `set_formula` | `name`, `value` | Add or replace a named formula |
| `remove_formula` | `name` | Remove a named formula |
| `put_view` | `name`, `fields` | Add a view or update the named fields of an existing view |
| `remove_view` | `name` | Remove a named view |

A filter value is a formula string or a group with one `and`, `or` or `not` key. The key has a list of filter values. A view name must identify one view for the tool to target it

The Base editor preserves comments and quotes on fields it does not change. It can normalize indentation. The server checks the file structure but does not execute formulas or render views
