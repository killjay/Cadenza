"""Supported profile authoring rules shared by creation and editing."""
PROFILE_INSTRUCTIONS = """
CONSTRAINED 2D PROFILES
Use kind="profile", operation="add" for an editable flat sketch. Its parameters:
  entities: [{id:"ent_outer", kind:"rectangle", operation:"add", x:0, y:0, width:100, height:60},
             {id:"ent_opening", kind:"circle", operation:"subtract", x:0, y:0, diameter:20}],
  constraints: [
    {id:"con_x",kind:"dimension",entity_id:"ent_outer",parameter:"x",value:0},
    {id:"con_y",kind:"dimension",entity_id:"ent_outer",parameter:"y",value:0},
    {id:"con_width",kind:"dimension",entity_id:"ent_outer",parameter:"width",value:100},
    {id:"con_height",kind:"dimension",entity_id:"ent_outer",parameter:"height",value:60},
    {id:"con_diameter",kind:"dimension",entity_id:"ent_opening",parameter:"diameter",value:20},
    {id:"con_center",kind:"coincident",entity_a:"ent_outer",entity_b:"ent_opening"}].
One outer rectangle/circle, followed by up to 15 circular openings, all inside and
non-touching. Width is local X, height local Y. Axis-aligned entities; the feature
placement defines the sketch plane. Values are mm. Dimensions fix a variable;
coincident means shared entity CENTRES. Together they must fully constrain all
rectangle x/y/width/height or circle x/y/diameter variables before a solid build.
Entity values are initial seeds, not constraints. Server recomputes solution/status;
never patch solution or invent fully-constrained status. Preserve constraint IDs.
Conflicting dimensions reject the candidate. Ask which requirement may change;
never delete a constraint silently to get a build. General lines/arcs/tangency are unsupported.

Follow the profile with kind="extrude", parameters={target_feature:<profile>, amount:8},
operation="add" for stock or "subtract" for a cut into an existing solid. Positive
amount follows the profile normal. Move/rotate the PROFILE; extrusion placement stays
identity. Twist is unsupported. Initial Draftsman target_feature uses the exact earlier
profile name; Machinist uses its stable feature ID. Existing legacy polygon `sketch`
with height>0 remains a solid; height=0 is a flat polygon consumable by extrusion.

Edit a sketch dimension at /features/<profile_id>/parameters/constraints/<con_id>/value,
not entity seeds. To preserve centring, keep the coincident constraint when resizing.
A named parameter binding uses {feature_id:<profile>, constraint_id:"con_width",
target:"value",expression:"plate_width"}. Extrusion thickness binds target:"amount".
For bound dimensions patch the named driver's value; never overwrite its constraint.
Keep unbound dimensions and unrelated features unchanged. Report explicit assumptions.
"""
