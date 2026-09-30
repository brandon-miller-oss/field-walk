# Field Walk

WebXR BIM walkthrough viewer with clash review, structured issues, and BCF / report export.

- `index.html`: launch page. Lists every project in `projects/index.json`.
- `viewer.html?p=<id>`: opens one project.
- `projects/<id>/`: one folder per project (manifest, level files, thumbnail).
- `add_project.py`: turns IFC files into a new project folder and adds it to the launch page.

## Hosting
Upload the whole folder to any HTTPS host (GitHub Pages, Netlify, Cloudflare Pages).
HTTPS is required for VR. Opening the files straight from disk will not work,
because browsers block loading project files over file://.

This site is public unless the host requires a login. Only upload sample or approved
models; client models need an access-controlled host (for example Cloudflare Pages
with Cloudflare Access).

## Adding a project
    pip install ifcopenshell fast_simplification python-fcl numpy
    python add_project.py --id myproject --name "My Project" \
        arch=Arch.ifc struct=Struct.ifc hvac=Mech.ifc plumb=Plumb.ifc elec=Elec.ifc

Disciplines: arch, struct, hvac (or mech), plumb, elec, fire, site. Use any subset.
Levels are detected from the IFC storeys; systems are coloured from Revit's exported
systems. Then upload the new `projects/<id>/` folder and the updated
`projects/index.json`. Re-running with the same --id replaces that project.
Large models take a while: Snowdon Towers (six files, 330 MB) takes roughly 15 minutes.
