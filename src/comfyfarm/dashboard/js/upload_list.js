// The upload list on Models (the operator's own picks): what stays in it. No imports — tested under node.
// A row: {file, folder, state: staged|uploading|done|failed|cancelled, received, error, resume?}
// Field (dev.24): rows of a finished batch stayed, with no way to remove them but Clear — and Clear took the files just
// picked for the next batch with them. Now a batch drops its done rows when it ends (they are in the models table), and
// Clear drops only what is over (done, failed, cancelled): files picked and not sent yet stay.

export const isOver = (x) => x.state === "done" || x.state === "failed" || x.state === "cancelled";
export const isWaiting = (x) => x.state === "staged" || x.state === "failed";

function dropWhere(list, pred) {
  let n = 0;
  for (let i = list.length - 1; i >= 0; i--) if (pred(list[i])) { list.splice(i, 1); n++; }
  return n;
}
export const dropDone = (list) => dropWhere(list, (x) => x.state === "done");
export const clearOver = (list) => dropWhere(list, isOver);
