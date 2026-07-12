import os
import json
import argparse
import ifcopenshell
import ifcopenshell.util.element
from tqdm import tqdm

DEFAULT_CONFIG = {
    "pset_metadata": {
        "space_common": "Pset_SpaceCommon",
        "space_quantities": "Qto_SpaceBaseQuantities",
        "wall_common": "Pset_WallCommon",
        "wall_quantities": "Qto_WallBaseQuantities",
        "is_external": "IsExternal",
        "thermal_transmittance": "ThermalTransmittance",
        "length": "Length",
        "height": "Height"
    }
}

def load_or_create_config(config_path):
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            return config
        except Exception:
            return DEFAULT_CONFIG
    else:
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except Exception:
            pass
        return DEFAULT_CONFIG

def generate_sample_idf(output_path):
    """
    에러가 수정된 EnergyPlus v25.2 완벽 호환 샘플 IDF 파일 생성
    - 활동량(Activity Level) 스케줄 추가
    - Zone Air Node Name 필수 입력 속성 추가
    """
    with open(output_path, 'w', encoding='utf-8') as idf:
        idf.write("Version, 25.2;\n\n")
        idf.write("SimulationControl, No, No, No, No, Yes;\n\n")
        idf.write("Timestep, 4;\n\n")
        idf.write("RunPeriod, Annual_Sim, 1, 1, , 12, 31, , Tuesday, Yes, Yes, No, Yes, Yes;\n\n")
        idf.write("Building, Sample_Building, 0.0, Suburbs, 0.04, 0.4, FullExterior, 25, 6;\n\n")
        idf.write("GlobalGeometryRules, UpperLeftCorner, CounterClockWise, Relative, Relative, Relative;\n\n")

        idf.write("Material, Sample_Concrete, MediumRough, 0.2, 1.63, 2300, 1000, 0.9, 0.7, 0.7;\n\n")
        idf.write("Construction, Sample_Const, Sample_Concrete;\n\n")

        idf.write("Zone, Sample_Zone, 0, 0, 0, 0, 1, 1, 3.0, 75.0, 25.0;\n\n")

        # 4면 벽체
        wall_coords = [
            [(0,0,3), (0,0,0), (5,0,0), (5,0,3)], # South
            [(5,0,3), (5,0,0), (5,5,0), (5,5,3)], # East
            [(5,5,3), (5,5,0), (0,5,0), (0,5,3)], # North
            [(0,5,3), (0,5,0), (0,0,0), (0,0,3)]  # West
        ]
        for i, coords in enumerate(wall_coords, start=1):
            idf.write(f"BuildingSurface:Detailed, Wall_{i}, Wall, Sample_Const, Sample_Zone, ,\n")
            idf.write(f"  Outdoors, , SunExposed, WindExposed, autocalculate, 4,\n")
            idf.write(f"  {coords[0][0]}, {coords[0][1]}, {coords[0][2]}, {coords[1][0]}, {coords[1][1]}, {coords[1][2]},\n")
            idf.write(f"  {coords[2][0]}, {coords[2][1]}, {coords[2][2]}, {coords[3][0]}, {coords[3][1]}, {coords[3][2]};\n\n")

        # 바닥과 지붕
        idf.write("BuildingSurface:Detailed, Roof_1, Roof, Sample_Const, Sample_Zone, ,\n")
        idf.write("  Outdoors, , SunExposed, WindExposed, autocalculate, 4,\n")
        idf.write("  0, 5, 3,  0, 0, 3,  5, 0, 3,  5, 5, 3;\n\n")
        
        idf.write("BuildingSurface:Detailed, Floor_1, Floor, Sample_Const, Sample_Zone, ,\n")
        idf.write("  Ground, , NoSun, NoWind, autocalculate, 4,\n")
        idf.write("  0, 0, 0,  0, 5, 0,  5, 5, 0,  5, 0, 0;\n\n")

        # 스케줄 (항상 켜짐, 난방/냉방, 활동량 추가)
        idf.write("ScheduleTypeLimits, Fraction, 0.0, 1.0, Continuous;\n")
        idf.write("ScheduleTypeLimits, Temperature, -60.0, 200.0, Continuous;\n")
        idf.write("ScheduleTypeLimits, ControlType, 0, 4, Discrete;\n")
        idf.write("ScheduleTypeLimits, AnyNumber, , , Continuous;\n\n") # 활동량용 제약
        
        idf.write("Schedule:Constant, Always_On, Fraction, 1.0;\n")
        idf.write("Schedule:Constant, Always_4, ControlType, 4;\n")
        idf.write("Schedule:Constant, Heat_Set_20, Temperature, 20.0;\n")
        idf.write("Schedule:Constant, Cool_Set_24, Temperature, 24.0;\n")
        idf.write("Schedule:Constant, Activity_Level, AnyNumber, 120.0;\n\n") # 120W (일반 사무 업무 발열량)

        # 내부 발열 (사람 활동량 스케줄 반영)
        idf.write("People,\n")
        idf.write("  Sample_People, Sample_Zone, Always_On, People/Area, , 0.1, , 0.3, , Activity_Level;\n\n")
        
        idf.write("Lights,\n")
        idf.write("  Sample_Lights, Sample_Zone, Always_On, Watts/Area, 10.0, , , 0.2, 0.2, 0.2, 1.0;\n\n")
        
        idf.write("ElectricEquipment,\n")
        idf.write("  Sample_Equip, Sample_Zone, Always_On, Watts/Area, 15.0, , , 0.3, 0.1, 0.0, 1.0;\n\n")

        # 가상 냉난방기 및 필수 Air Node Name 속성 반영
        idf.write("ZoneControl:Thermostat, Zone_Thermostat, Sample_Zone, Always_4, ThermostatSetpoint:DualSetpoint, Dual_Setpoint;\n\n")
        idf.write("ThermostatSetpoint:DualSetpoint, Dual_Setpoint, Heat_Set_20, Cool_Set_24;\n\n")
        
        idf.write("ZoneHVAC:EquipmentConnections,\n")
        idf.write("  Sample_Zone, Zone_Eq_List, Zone_Inlets, , Sample_Zone_Node, Sample_Zone_Return;\n\n") # Node 이름 명시
        
        idf.write("NodeList, Zone_Inlets, Ideal_Loads_Inlet;\n\n")
        idf.write("ZoneHVAC:EquipmentList, Zone_Eq_List, SequentialLoad, ZoneHVAC:IdealLoadsAirSystem, Ideal_Loads_System, 1, 1;\n\n")
        
        idf.write("ZoneHVAC:IdealLoadsAirSystem,\n")
        idf.write("  Ideal_Loads_System, , Ideal_Loads_Inlet, , , 50, 13, 0.015, 0.008, NoLimit, , , NoLimit, , ;\n\n")

        # 단위 변환 및 출력
        idf.write("OutputControl:Table:Style, Comma, JtoKWH;\n\n")
        idf.write("Output:Table:SummaryReports, AllSummary;\n\n")
        
    print(f"[Info] Default sample IDF successfully generated at: {output_path}")

def convert_ifc_to_idf(ifc_filepath, idf_filepath, config):
    try:
        ifc_file = ifcopenshell.open(ifc_filepath)
    except Exception as e:
        print(f"\n[Error] Failed to load {ifc_filepath}: {e}")
        return False

    pset_meta = config.get("pset_metadata", DEFAULT_CONFIG["pset_metadata"])
    p_space_qto = pset_meta.get("space_quantities", "Qto_SpaceBaseQuantities")
    p_wall_common = pset_meta.get("wall_common", "Pset_WallCommon")
    p_wall_qto = pset_meta.get("wall_quantities", "Qto_WallBaseQuantities")
    p_is_ext = pset_meta.get("is_external", "IsExternal")
    
    with open(idf_filepath, 'w', encoding='utf-8') as idf:
        idf.write("Version, 25.2;\n\n")
        idf.write("SimulationControl, No, No, No, No, Yes;\n\n")
        idf.write("Timestep, 4;\n\n")
        idf.write("RunPeriod, Annual_Simulation, 1, 1, , 12, 31, , Tuesday, Yes, Yes, No, Yes, Yes;\n\n")
        idf.write("Building, IFC_Converted_Building, 0.0, Suburbs, 0.04, 0.4, FullExterior, 25, 6;\n\n")
        idf.write("GlobalGeometryRules, UpperLeftCorner, CounterClockWise, Relative, Relative, Relative;\n\n")

        idf.write("Material, Default_Wall_Mat, MediumRough, 0.2, 1.63, 2300, 1000, 0.9, 0.7, 0.7;\n\n")
        idf.write("Construction, EXT_WALL_CONST, Default_Wall_Mat;\n\n")

        spaces = ifc_file.by_type("IfcSpace")
        for space in tqdm(spaces, desc="Processing Spaces", leave=False):
            space_name = space.Name if space.Name else f"Zone_{space.GlobalId}"
            safe_space_name = str(space_name).replace(" ", "_").replace(",", "_")
            
            psets = ifcopenshell.util.element.get_psets(space)
            space_qto = psets.get(p_space_qto, {})
            volume = space_qto.get("GrossVolume", space_qto.get("NetVolume", 100.0))
            area = space_qto.get("GrossFloorArea", space_qto.get("NetFloorArea", 50.0))
            
            idf.write(f"Zone, {safe_space_name}, 0, 0, 0, 0, 1, 1, autocalculate, {volume}, {area};\n\n")

        walls = ifc_file.by_type("IfcWallStandardCase")
        surface_count = 1
        for wall in tqdm(walls, desc="Processing Walls (StandardCase)", leave=False):
            psets = ifcopenshell.util.element.get_psets(wall)
            wall_common = psets.get(p_wall_common, {})
            if not wall_common.get(p_is_ext):
                continue 
                
            quantities = psets.get(p_wall_qto, {})
            length = quantities.get(pset_meta.get("length", "Length"))
            height = quantities.get(pset_meta.get("height", "Height"))
            
            if length and height:
                wall_name = f"Wall_{surface_count}_{wall.GlobalId}"
                
                idf.write(f"BuildingSurface:Detailed, {wall_name}, Wall, EXT_WALL_CONST, Zone_1, ,\n")
                idf.write(f"  Outdoors, , SunExposed, WindExposed, autocalculate, 4,\n")
                idf.write(f"  0, 0, {height},  0, 0, 0,  {length}, 0, 0,  {length}, 0, {height};\n\n")
                surface_count += 1
                
        idf.write("OutputControl:Table:Style, Comma, JtoKWH;\n\n")
        idf.write("Output:Table:SummaryReports, AllSummary;\n\n")
                
    return True

def main():
    parser = argparse.ArgumentParser(description="Convert IFC to IDF and generate a sample IDF file.")
    parser.add_argument("-i", "--input", type=str, default="./input", help="Input directory")
    parser.add_argument("-o", "--output", type=str, default="./output", help="Output directory")
    parser.add_argument("-c", "--config", type=str, default="./config.json", help="Path to config.json")
    
    args = parser.parse_args()

    # 1. 아웃풋 디렉토리 확인 및 생성
    if not os.path.exists(args.output):
        os.makedirs(args.output)
        print(f"[Info] Created output directory: '{args.output}'")

    # 2. sample.idf를 Output 폴더에 기본으로(무조건) 생성
    sample_path = os.path.join(args.output, "sample.idf")
    generate_sample_idf(sample_path)

    # 3. 설정 파일 로드
    config = load_or_create_config(args.config)

    # 4. Input 폴더 내 IFC 파일 변환 로직
    if not os.path.exists(args.input):
        print(f"[Info] Input directory '{args.input}' does not exist. Skipping IFC conversion.")
        return

    ifc_files = [f for f in os.listdir(args.input) if f.lower().endswith('.ifc')]
    if not ifc_files:
        print(f"[Info] No IFC files found in '{args.input}'. Only sample.idf was generated.")
        return

    print(f"[Info] Found {len(ifc_files)} IFC file(s). Starting conversion...\n")
    for filename in tqdm(ifc_files, desc="Overall Progress"):
        ifc_path = os.path.join(args.input, filename)
        idf_path = os.path.join(args.output, f"{os.path.splitext(filename)[0]}.idf")
        
        if not convert_ifc_to_idf(ifc_path, idf_path, config):
            print(f"\n[Warning] Conversion failed for {filename}")

    print("\n[Success] Process Finished.")

if __name__ == "__main__":
    main()