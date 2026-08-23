# Engineering drawing clockwise numbering

Use [clockwise_numbering.py](clockwise_numbering.py) for a new drawing.

```powershell
# Best option: use reviewed items for correct engineering values and leader targets.
python .\clockwise_numbering.py .\drawing.jpg --items .\reviewed_items.json --output-dir .\result

# OCR draft only: review `result\ocr_review_items.json` before release.
python .\clockwise_numbering.py .\drawing.jpg --ocr --output-dir .\result
```

Copy and edit [items_example.json](items_example.json) to make `reviewed_items.json`.
The script applies these rules: material description/specification first; grouped
drawing callouts clockwise; final mass before the note; note last.  Add `view`
to finish one view before the next, and add `order` to override clockwise order
for a drawing-specific inspection sequence.

OCR requires Tesseract and `pytesseract`; its extracted dimensions and tolerances
must be reviewed before use.
