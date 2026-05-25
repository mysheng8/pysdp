"""Auto-generated skill: Mesh Ratio"""
from chat.skills import SkillContext


def run(ctx: SkillContext):
    db = ctx.db
    snapshot_ids = ctx.snapshot_ids
    snapshot_id = snapshot_ids[0] if snapshot_ids else None
    from data import query as data_query


    dcs = data_query.get_draw_calls(db, snapshot_id)
    scene_dcs = [d for d in dcs if d.get('category') == 'Scene']

    total_faces = sum(d.get('index_count', 0) or d.get('vertex_count', 0) for d in dcs)
    scene_faces_sum = sum(d.get('index_count', 0) or d.get('vertex_count', 0) for d in scene_dcs)

    scene_avg_faces = scene_faces_sum / len(scene_dcs) if scene_dcs else 0
    ratio = (scene_avg_faces / total_faces) if total_faces else 0

    print(f"Scene category 的平均面数占总面数的比例是 **{ratio:.2%}**.")
    print(f"其中：")
    print(f"- Scene category 的平均面数: {scene_avg_faces:.2f}")
    print(f"- 总面数: {total_faces}")
