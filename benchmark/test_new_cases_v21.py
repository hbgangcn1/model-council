"""v21 新增 12 客观题离线测试（R5 R6 T4-T6 C5-C7 L5 L6 I5 I6）。

全离线：无 API/LLM 调用，只读 v21-cases.json + test-sandbox 文件。
覆盖：
1. 用例集总数恰 +12（52）、新 ID 齐备且 scoring=objective+带 scoringSpec；
2. contentHash 与 hashedFields 一致（golden_evolve._v21_hash 同算法）；
3. 每题：正确 fixture 经 scorer 判满分（>=8），故意答错 fixture 判 fail（<=4）；
4. T4-T6 期望答案可从沙箱文件重新算出（答案唯一且锚定文件内容）；
5. R5/R6 解唯一（程序穷举/方程组）；C5-C7 硬约束（字数/词数/押韵对）可脚本校验。

pytest 或 python 直接跑均可。
"""
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import scorer as scorer_mod  # noqa: E402

BASE = Path(__file__).resolve().parent
CASES_FILE = BASE / "v21-cases.json"
SANDBOX = BASE / "test-sandbox"

NEW_IDS = ["R5", "R6", "T4", "T5", "T6", "C5", "C6", "C7", "L5", "L6", "I5", "I6"]

# id -> (正确 fixture, 错误 fixture)
FIXTURES = {
    "R5": ("设百位A、十位B、个位C。由A+B+C=14、A=2C、B=C+2，代入得C=3，B=5，A=6，"
           "无重复且6>5，故唯一满足全部约束的组合是653。",
           "逐项验证后，满足约束的组合是635。"),
    "R6": ("设丙x岁，则乙x+4岁，甲x+10岁，和3x+14=62，x=16。"
           "故甲26岁，乙20岁，丙16岁，甲为偶数符合。",
           "解得甲24岁，乙20岁，丙18岁。"),
    "T4": ("①原价总货值13300元 ②促销后总货值12600元",
           "①原价总货值12000元 ②促销后11000元"),
    "T5": ("ERROR行数最多的是svc_a.log，共7行；三个文件ERROR行共15行。",
           "svc_b.log最多，共3行，三文件总数11行。"),
    "T6": ("应用版本3.4.1，入口文件main.py，t6目录下共5个文件。",
           "应用版本2.0.0，入口文件app.py，t6下共4个文件。"),
    "C5": ("风吹山上青松翠\n花落翠竹林边路\n雪映寒梅岭上香\n月照幽兰谷中静\n"
           "共4句\n每句7字\n首字连读：风花雪月",
           "风吹山上青松翠\n花落翠竹林边路\n雪映寒梅岭上香"),
    "C6": ("日出群山金光照大地明\n月落沧海波光静夜深寒\n星垂寥空万里一色秋明\n"
           "共3句\n每句10字\n首字连读：日月星",
           "日出群山金光照大地明\n月落沧海波光静夜深寒"),
    "C7": ("The old river winds through valleys green today\n"
           "We sailed at dusk toward the fading light\n"
           "Above the sleepy town stands one dark mountain\n"
           "And dreams return to us again at night\n"
           "共4行：8 8 8 8",
           "The old river winds through valleys green today\n"
           "We sailed at dusk toward the fading light\n"
           "And dreams return to us again at night"),
    "L5": ("①Q2营收300万元 ②Q3营收480万元 ③Q1-Q3合计1020万元",
           "①Q2营收280万元 ②Q3营收450万元 ③合计970万元"),
    "L6": ("发现两处矛盾：矛盾1在第2段9.6亿元与第5段6.9亿元；"
           "矛盾2在第3段850人与第6段580人。",
           "第2段与第5段数字不同。"),
    "I5": ("第一句：晨起榕树下跑步。\n第二句：夜里雨水洗过小径。\n"
           "第三句：湖边长椅坐满老人。\n第四句：午后蝉鸣一阵高过一阵。\n"
           "第五句：傍晚晚风带来花香。\n共5句",
           "第一句：晨起榕树下跑步。\n第二句：夜里雨水洗过小径。\n"
           "第三句：湖边长椅坐满老人。\n展望未来会更好。"),
    "I6": ("name,age,city\nLi Na,28,Hangzhou\nChen Bo,35,Suzhou\nZhao Lin,41,Nanjing",
           "```csv\nname,age,city\nLi Na,29,Hangzhou\nChen Bo,36,Suzhou\n```"),
}

FAILED = []


def check(name, cond):
    if cond:
        print(f"  ok {name}")
    else:
        print(f"  FAIL {name}")
        FAILED.append(name)


def _load():
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def test_case_set_grew_by_12():
    print("[用例集 +12]")
    doc = _load()
    check("totalCases=52", doc["totalCases"] == 52 == len(doc["cases"]))
    by_id = {c["id"]: c for c in doc["cases"]}
    check("12 新 ID 齐备", all(i in by_id for i in NEW_IDS))
    check("新题全 objective", all(by_id[i].get("scoring") == "objective" for i in NEW_IDS))
    check("新题全带 scoringSpec", all(isinstance(by_id[i].get("scoringSpec"), dict) for i in NEW_IDS))
    dims = {"R5": "reasoning", "R6": "reasoning", "T4": "tool_use", "T5": "tool_use",
            "T6": "tool_use", "C5": "creativity", "C6": "creativity", "C7": "creativity",
            "L5": "long_context", "L6": "long_context",
            "I5": "instruction_following", "I6": "instruction_following"}
    check("维度归属正确", all(by_id[i].get("dimension") == d for i, d in dims.items()))


def test_content_hash():
    print("[contentHash]")
    doc = _load()
    fields = doc.get("hashedFields") or ["cases"]
    payload = {f: doc.get(f) for f in fields if f in doc}
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expect = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    check("contentHash 与 cases 一致", doc.get("contentHash") == expect)


def test_scorer_discriminates():
    print("[scorer 正确满分/错误判fail]")
    doc = _load()
    by_id = {c["id"]: c for c in doc["cases"]}
    for cid in NEW_IDS:
        good, bad = FIXTURES[cid]
        gs, gnote, _ = scorer_mod.score_objective_with_spec(by_id[cid], good, {})
        bs, bnote, _ = scorer_mod.score_objective_with_spec(by_id[cid], bad, {})
        check(f"{cid} 正确 fixture>=8（得{gs}）", gs is not None and gs >= 8)
        check(f"{cid} 错误 fixture<=4（得{bs}）", bs is not None and bs <= 4)


def test_tool_answers_anchored_in_files():
    print("[T4-T6 答案锚定沙箱文件]")
    stock = json.loads((SANDBOX / "t4" / "stock.json").read_text(encoding="utf-8"))
    prices = {}
    for ln in (SANDBOX / "t4" / "prices.csv").read_text(encoding="utf-8").splitlines()[1:]:
        k, v = ln.split(",")
        prices[k] = float(v)
    full = sum(s["qty"] * prices[s["sku"]] for s in stock)
    disc = sum(s["qty"] * prices[s["sku"]] * (0.8 if s["sku"] == "C03" else 1.0) for s in stock)
    check(f"T4 文件重算=13300/12600（得{full}/{disc}）", full == 13300 and disc == 12600)
    counts = {}
    for f in ["svc_a.log", "svc_b.log", "svc_c.log"]:
        counts[f] = sum(1 for ln in (SANDBOX / "t5" / f).read_text(encoding="utf-8").splitlines()
                        if "ERROR" in ln)
    check(f"T5 文件重算 a=7/b=3/c=5/共15（得{counts}）",
          counts == {"svc_a.log": 7, "svc_b.log": 3, "svc_c.log": 5} and sum(counts.values()) == 15)
    t6files = sorted(p.name for p in (SANDBOX / "t6").iterdir() if p.is_file())
    ver = re.search(r"version:\s*(\S+)", (SANDBOX / "t6" / "meta.yaml").read_text(encoding="utf-8")).group(1)
    check(f"T6 文件重算 v3.4.1/main.py/5文件（得{ver}/{t6files}）",
          ver == "3.4.1" and "main.py" in t6files and len(t6files) == 5)


def test_puzzle_uniqueness():
    print("[R5/R6 解唯一]")
    sols = [f"{a}{b}{c}" for a in range(1, 10) for b in range(10) for c in range(10)
            if a + b + c == 14 and a == 2 * c and b == c + 2 and len({a, b, c}) == 3 and a > b]
    check(f"R5 穷举唯一解=653（得{sols}）", sols == ["653"])
    # R6：三元一次方程组唯一解
    sols6 = [(a, b, c) for a in range(200) for b in range(200) for c in range(200)
             if a == b + 6 and b == c + 4 and a + b + c == 62 and a % 2 == 0]
    check(f"R6 唯一解=(26,20,16)（得{sols6}）", sols6 == [(26, 20, 16)])
    check("L5 链算 300/480/1020", 240 * 1.25 == 300 and 300 * 2 - 240 / 2 == 480
          and 240 + 300 + 480 == 1020)


def test_creativity_hard_constraints():
    print("[C5-C7 硬约束可脚本校验]")
    good5 = FIXTURES["C5"][0].splitlines()
    check("C5 4句×7字", len(good5) >= 4 and all(len(good5[i]) == 7 for i in range(4)))
    check("C5 首字风花雪月", "".join(good5[i][0] for i in range(4)) == "风花雪月")
    check("C5 松竹梅兰各在其句", all(w in good5[i] for i, w in enumerate(["松", "竹", "梅", "兰"])))
    good6 = FIXTURES["C6"][0].splitlines()
    check("C6 3句×10字", len(good6) >= 3 and all(len(good6[i]) == 10 for i in range(3)))
    check("C6 首字日月星", "".join(good6[i][0] for i in range(3)) == "日月星")
    check("C6 山海空各在其句", all(w in good6[i] for i, w in enumerate(["山", "海", "空"])))
    good7 = FIXTURES["C7"][0].splitlines()
    check("C7 4行×8词", len(good7) >= 4 and all(len(good7[i].split()) == 8 for i in range(4)))
    check("C7 river/mountain/light/night",
          "river" in good7[0] and good7[1].endswith("light")
          and "mountain" in good7[2] and good7[3].endswith("night"))
    # 押韵字典检查：light/night 同韵尾 ight
    check("C7 light/night 押韵（同韵尾）", good7[1].split()[-1][-4:] == good7[3].split()[-1][-4:] == "ight")


def main():
    test_case_set_grew_by_12()
    test_content_hash()
    test_scorer_discriminates()
    test_tool_answers_anchored_in_files()
    test_puzzle_uniqueness()
    test_creativity_hard_constraints()
    print()
    if FAILED:
        print(f"FAIL {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL GREEN")


# pytest 兼容
def test_new_cases_pytest():
    n0 = len(FAILED)
    test_case_set_grew_by_12()
    test_content_hash()
    test_scorer_discriminates()
    test_tool_answers_anchored_in_files()
    test_puzzle_uniqueness()
    test_creativity_hard_constraints()
    assert len(FAILED) == n0


if __name__ == "__main__":
    main()
